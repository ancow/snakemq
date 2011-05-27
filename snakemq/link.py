# -*- coding: utf-8 -*-
"""
:author: David Siroky (siroky@dasir.cz)
:license: MIT License (see LICENSE.txt or
          U{http://www.opensource.org/licenses/mit-license.php})
"""

# TODO unify socket and SSLSocket under a single class for easier mapping
# socket:conn_id and other stuff

import select
import socket
import ssl
import os
import errno
import time
import bisect
import logging

if os.name == "nt":
    from snakemq.winpoll import Epoll
    epoll = Epoll
else:
    epoll = select.epoll

from snakemq.pollbell import Bell
from snakemq.callbacks import Callback

############################################################################
############################################################################

RECONNECT_INTERVAL = 3.0
RECV_BLOCK_SIZE = 256 * 1024
POLL_TIMEOUT = 0.2
BELL_READ = 1024

SSL_HANDSHAKE_IN_PROGRESS = 0
SSL_HANDSHAKE_DONE = 1
SSL_HANDSHAKE_FAILED = 2

############################################################################
############################################################################

def _create_ssl_context(sock):
    # XXX why Python's ssl module does not support nonblocking sockets?!?!?
    if hasattr(sock, "context"):
        # py3.2
        sock._sslobj = sock.context._wrap_socket(sock, sock.server_side, None)
    else:
        if hasattr(sock, "_sock"):
            raw_sock = sock._sock  # py2
        else:
            raw_sock = sock  # py3.1
        sock._sslobj = ssl._ssl.sslwrap(raw_sock, False,
                                        sock.keyfile, sock.certfile,
                                        sock.cert_reqs, sock.ssl_version,
                                        sock.ca_certs)

############################################################################
############################################################################

class SSLConfig(object):
    """
    Container for SSL configuration.
    """
    def __init__(self, keyfile=None, certfile=None, cert_reqs=ssl.CERT_NONE,
                  ssl_version=ssl.PROTOCOL_SSLv23, ca_certs=None):
        """
        :see: ssl.wrap_socket
        """
        self.keyfile = keyfile
        self.certfile = certfile
        self.cert_reqs = cert_reqs
        self.ssl_version = ssl_version
        self.ca_certs = ca_certs

############################################################################
############################################################################

class Link(object):
    """
    Just a bare wire stream communication. Keeper of opened (TCP) connections.
    **Not thread-safe** but you can synchronize with the loop using
    :meth:`~.wakeup_poll` and :attr:`~.on_loop_pass`.
    """

    def __init__(self):
        self.log = logging.getLogger("snakemq.link")

        self.reconnect_interval = RECONNECT_INTERVAL  #: in seconds
        self.recv_block_size = RECV_BLOCK_SIZE

        #{ callbacks
        self.on_connect = Callback()  #: ``func(conn_id)``
        self.on_disconnect = Callback()  #: ``func(conn_id)``
        #: ``func(conn_id, data)``
        self.on_recv = Callback()
        #: ``func(conn_id)``, last send was successful
        self.on_ready_to_send = Callback()
        #: ``func()``, called after poll is processed
        self.on_loop_pass = Callback()
        #}

        self._do_loop = False  #: False breaks the loop

        self._new_conn_id = 0  #: counter for conn id generator

        self.poller = epoll()
        self._poll_bell = Bell()
        self.log.debug("poll bell fd=%r" % self._poll_bell)
        self.poller.register(self._poll_bell.r, select.EPOLLIN)  # read part

        self._sock_by_fd = {}
        self._conn_by_sock = {}
        self._sock_by_conn = {}

        self._listen_socks = {}  #: address:sock
        self._listen_socks_filenos = set()

        self._connectors = {}  #: address:sock
        self._socks_addresses = {}  #: sock:address
        self._socks_waiting_to_connect = set()
        self._plannned_connections = []  #: (when, address)
        self._reconnect_intervals = {}  #: address:interval

        self._ssl_config = {}  #: key:config, key is address or socket
        self._in_ssl_handshake = set()  #: set of SSLSockets

    ##########################################################

    def cleanup(self):
        """
        Close all sockets and remove all connectors and listeners.
        """
        assert not self._do_loop

        self._poll_bell.close()

        for address in list(self._connectors.keys()):
            self.del_connector(address)

        for address in list(self._listen_socks.keys()):
            self.del_listener(address)

        for sock in list(self._sock_by_fd.values()):
            self.handle_close(sock)

        # be sure that no memory is wasted
        assert len(self._sock_by_fd) == 0
        assert len(self._conn_by_sock) == 0
        assert len(self._sock_by_conn) == 0
        assert len(self._listen_socks) == 0
        assert len(self._listen_socks_filenos) == 0
        assert len(self._connectors) == 0
        assert len(self._socks_addresses) == 0
        assert len(self._socks_waiting_to_connect) == 0
        assert len(self._plannned_connections) == 0
        assert len(self._reconnect_intervals) == 0
        assert len(self._ssl_config) == 0
        assert len(self._in_ssl_handshake) == 0

    ##########################################################

    def add_connector(self, address, reconnect_interval=None, ssl_config=None):
        """
        This will not create an immediate connection. It just adds a connector
        to the pool.

        :param address: remote address
        :param reconnect_interval: reconnect interval in seconds
        :return: connector address (use it for deletion)
        """
        address = socket.gethostbyname(address[0]), address[1]
        if ssl_config is not None:
            self._ssl_config[address] = ssl_config
        if address in self._connectors:
            raise ValueError("connector '%r' already set", address)
        self._connectors[address] = None  # no socket yet
        self._reconnect_intervals[address] = \
                reconnect_interval or self.reconnect_interval
        self.plan_connect(0, address)  # connect ASAP
        return address

    ##########################################################

    def del_connector(self, address):
        """
        Delete connector.
        """
        sock = self._connectors.pop(address)
        self._socks_waiting_to_connect.discard(sock)
        del self._reconnect_intervals[address]
        if address in self._ssl_config:
            del self._ssl_config[address]

        # filter out address from plan
        self._plannned_connections[:] = \
            [(when, _address)
                  for (when, _address) in self._plannned_connections
                  if _address != address]

    ##########################################################

    def add_listener(self, address, ssl_config=None):
        """
        Adds listener to the pool. This method is not blocking. Run only once.

        :return: listener address (use it for deletion)
        """
        address = socket.gethostbyname(address[0]), address[1]
        if address in self._listen_socks:
            raise ValueError("listener '%r' already set" % address)
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fileno = listen_sock.fileno()
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind(address)
        listen_sock.listen(10)
        listen_sock.setblocking(0)

        if ssl_config is not None:
            self._ssl_config[listen_sock] = ssl_config
        self._sock_by_fd[fileno] = listen_sock
        self._listen_socks[address] = listen_sock
        self._listen_socks_filenos.add(fileno)
        self.poller.register(listen_sock, select.EPOLLIN)

        self.log.debug("add_listener fd=%i %r" % (fileno, address))
        return address

    ##########################################################

    def del_listener(self, address):
        """
        Delete listener.
        """
        sock = self._listen_socks.pop(address)
        fileno = sock.fileno()
        self._listen_socks_filenos.remove(fileno)
        del self._sock_by_fd[fileno]
        if sock in self._ssl_config:
            del self._ssl_config[sock]
        sock.close()

    ##########################################################

    def wakeup_poll(self):
        """
        Thread-safe.
        """
        self._poll_bell.write(b"a")

    ##########################################################

    def send(self, conn_id, data):
        """
        .. warning::
          this operation is non-blocking, data might be lost if you close
          connection before proper delivery. Always wait for
          :py:attr:`~.on_ready_to_send` to have confirmation about successful
          send.

        :return: number of bytes sent
        """
        try:
            sock = self._sock_by_conn[conn_id]
            sent_length = sock.send(data)
            self.poller.modify(sock, select.EPOLLIN | select.EPOLLOUT)
        except socket.error as exc:
            err = exc.args[0]
            if err == errno.EWOULDBLOCK:
                return 0
            elif err in (errno.ECONNRESET, errno.ENOTCONN, errno.ESHUTDOWN,
                        errno.ECONNABORTED, errno.EPIPE, errno.EBADF):
                self.handle_close(sock)
                return 0
            else:
                raise

        return sent_length

    ##########################################################

    def close(self, conn_id):
        self.handle_close(self._sock_by_conn[conn_id])

    ##########################################################

    def loop(self, poll_timeout=POLL_TIMEOUT, count=None, runtime=None):
        """
        Start the communication loop.

        :param poll_timeout: in seconds, should be less then the minimal
                              reconnect time
        :param count: count of poll events (not timeouts) or None
        :param runtime: max time of running loop in seconds (also depends
                        on the poll timeout) or None
        """
        self._do_loop = True

        # plan fresh connects
        self.deal_connects()

        time_start = time.time()
        while (self._do_loop and
                (count is not 0) and
                not ((runtime is not None) and
                      (time.time() - time_start > runtime))):
            is_event = len(self.loop_pass(poll_timeout))
            self.on_loop_pass()
            if is_event and (count is not None):
                count -= 1

        self._do_loop = False

    ##########################################################

    def stop(self):
        """
        Interrupt the loop. It doesn't perform a cleanup.
        """
        self._do_loop = False

    ##########################################################

    def get_socket_by_conn(self, conn):
        return self._sock_by_conn[conn]

    ##########################################################
    ##########################################################

    def new_connection_id(self, sock):
        """
        Create a virtual connection ID. This ID will be passed to ``on_*``
        functions. It is a unique identifier for every new connection during
        the instance's existence.
        """
        # NOTE e.g. pair address+port can't be used as a connection identifier
        # because it is not unique enough. It might be the same for 2 connections
        # distinct in time.

        self._new_conn_id += 1
        conn_id = "%ifd%i" % (self._new_conn_id, sock.fileno())
        self._conn_by_sock[sock] = conn_id
        self._sock_by_conn[conn_id] = sock
        return conn_id

    ##########################################################

    def del_connection_id(self, sock):
        conn_id = self._conn_by_sock.pop(sock)
        del self._sock_by_conn[conn_id]

    ##########################################################

    def plan_connect(self, when, address):
        item = (when, address)
        idx = bisect.bisect(self._plannned_connections, item)
        self._plannned_connections.insert(idx, item)

    ##########################################################

    def _connect(self, address):
        """
        Try to make an actual connection.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(0)
        ssl_config = self._ssl_config.get(address)
        if ssl_config:
            sock = ssl.wrap_socket(sock, server_side=False,
                                      do_handshake_on_connect=False,
                                      ssl_version=ssl_config.ssl_version,
                                      keyfile=ssl_config.keyfile,
                                      certfile=ssl_config.certfile,
                                      cert_reqs=ssl_config.cert_reqs,
                                      ca_certs=ssl_config.ca_certs)
        self.poller.register(sock)

        self._sock_by_fd[sock.fileno()] = sock
        self._connectors[address] = sock
        self._socks_addresses[sock] = address
        self._socks_waiting_to_connect.add(sock)

        err = sock.connect_ex(address)
        if err in (0, errno.EISCONN):
            self.handle_connect(sock)
        elif err not in (errno.EINPROGRESS, errno.EWOULDBLOCK):
            raise socket.error(err, errno.errorcode[err])

    ##########################################################

    def ssl_handshake(self, sock):
        failed = False
        err = None

        try:
            sock.do_handshake()
        except ssl.SSLError as err:
            if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                self.poller.modify(sock, select.EPOLLIN)
            elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                self.poller.modify(sock, select.EPOLLOUT)
            else:
                failed = True
        except socket.error as err:
            failed = True
        else:
            self._in_ssl_handshake.remove(sock)
            self.poller.modify(sock, select.EPOLLIN)
            self.log.debug("SSL handshake done, fd=%i, cipher=%r" %
                            (sock.fileno(), sock.cipher()))
            return SSL_HANDSHAKE_DONE
        
        if failed:
            self.log.error("SSL handshake fd=%i: %r" % (sock.fileno(), err))
            self.handle_close(sock)
            return SSL_HANDSHAKE_FAILED

        return SSL_HANDSHAKE_IN_PROGRESS

    ##########################################################

    def handle_connect(self, sock):
        self._socks_waiting_to_connect.remove(sock)

        is_ssl = isinstance(sock, ssl.SSLSocket)
        handshake_res = SSL_HANDSHAKE_IN_PROGRESS
        if is_ssl:
            _create_ssl_context(sock)
            self._in_ssl_handshake.add(sock)
            handshake_res = self.ssl_handshake(sock)
            if handshake_res == SSL_HANDSHAKE_FAILED:
                return

        conn_id = self.new_connection_id(sock)
        self.log.info("connect %s %r" % (conn_id, sock.getpeername()))
        self.poller.modify(sock, select.EPOLLIN)

        if not is_ssl or (handshake_res == SSL_HANDSHAKE_DONE):
            self.on_connect(conn_id)

    ##########################################################

    def handle_accept(self, sock):
        try:
            newsock, address = sock.accept()
        except socket.error as exc:
            self.log.error("accept %r: %r" % (sock, exc))
            return
        newsock.setblocking(0)

        ssl_config = self._ssl_config.get(sock)
        handshake_res = SSL_HANDSHAKE_IN_PROGRESS
        if ssl_config:
            newsock = ssl.wrap_socket(newsock, server_side=True,
                                      do_handshake_on_connect=False,
                                      ssl_version=ssl_config.ssl_version,
                                      keyfile=ssl_config.keyfile,
                                      certfile=ssl_config.certfile,
                                      cert_reqs=ssl_config.cert_reqs,
                                      ca_certs=ssl_config.ca_certs)
            self.poller.register(newsock, select.EPOLLIN)
            self._in_ssl_handshake.add(newsock)
            # replace the plain socket
            self._sock_by_fd[newsock.fileno()] = newsock
            handshake_res = self.ssl_handshake(newsock)
            if handshake_res == SSL_HANDSHAKE_FAILED:
                return
        else:
            self.poller.register(newsock, select.EPOLLIN)
            self._sock_by_fd[newsock.fileno()] = newsock

        conn_id = self.new_connection_id(newsock)
        self.log.info("accept %s %r" % (conn_id, address))

        if not ssl_config or (handshake_res == SSL_HANDSHAKE_DONE):
            self.on_connect(conn_id)

    ##########################################################

    def handle_recv(self, sock):
        conn_id = self._conn_by_sock[sock]

        # do not put it in a draining cycle to avoid other links starvation
        try:
            fragment = sock.recv(self.recv_block_size)
            if fragment:
                self.log.debug("recv %s len=%i" % (conn_id, len(fragment)))
                self.on_recv(conn_id, fragment)
            else:
                self.handle_close(sock)
        except socket.error as exc:
            # TODO catch SSL exceptions
            err = exc.args[0]
            if err in (errno.ECONNRESET, errno.ENOTCONN, errno.ESHUTDOWN,
                        errno.ECONNABORTED, errno.EPIPE, errno.EBADF):
                self.log.error("recv %s error %s" %
                                  (conn_id, errno.errorcode[err]))
                self.handle_close(sock)
            elif err != errno.EWOULDBLOCK:
                raise

    ##########################################################

    def handle_conn_refused(self, sock):
        self._socks_waiting_to_connect.remove(sock)
        self.poller.unregister(sock)
        del self._sock_by_fd[sock.fileno()]
        sock.close()

        address = self._socks_addresses.pop(sock)
        self.plan_connect(time.time() + self._reconnect_intervals[address],
                          address)

    ##########################################################

    def handle_close(self, sock):
        self.poller.unregister(sock)
        del self._sock_by_fd[sock.fileno()]
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except socket.error as exc:
            if exc.errno != errno.ENOTCONN:
                raise
        sock.close()

        if sock in self._conn_by_sock:
            conn_id = self._conn_by_sock[sock]
            self.del_connection_id(sock)
            self.log.info("disconnect %s " % conn_id)
            if ((not isinstance(sock, ssl.SSLSocket)) or
                    (sock not in self._in_ssl_handshake)):
                self.on_disconnect(conn_id)

        if sock in self._socks_addresses:
            address = self._socks_addresses.pop(sock)
            interval = self._reconnect_intervals.get(address)
            if interval:
                self.plan_connect(time.time() + interval, address)

    ##########################################################

    def handle_ready_to_send(self, sock):
        self.poller.modify(sock, select.EPOLLIN)
        conn_id = self._conn_by_sock[sock]
        self.log.debug("ready to send " + conn_id)
        self.on_ready_to_send(conn_id)

    ##########################################################

    def handle_sock_err(self, sock):
        if sock in self._socks_waiting_to_connect:
            self.handle_conn_refused(sock)
        else:
            self.handle_close(sock)

    ##########################################################

    def handle_sock_io(self, fd, sock, mask):
        if mask & select.EPOLLOUT:
            if sock in self._socks_waiting_to_connect:
                self.handle_connect(sock)
            else:
                self.handle_ready_to_send(sock)
        if mask & select.EPOLLIN:
            if fd in self._listen_socks_filenos:
                self.handle_accept(sock)
            else:
                self.handle_recv(sock)

    ##########################################################

    def handle_fd_mask(self, fd, mask):
        if fd == self._poll_bell.r:
            assert mask & select.EPOLLIN
            self._poll_bell.read(BELL_READ)  # flush the pipe
        else:
            # socket might have been already discarded by the Link
            # so this pass might be skipped
            if fd not in self._sock_by_fd:
                return
            sock = self._sock_by_fd[fd]

            if mask & (select.EPOLLERR | select.EPOLLHUP):
                self.handle_sock_err(sock)
            else:
                if sock in self._in_ssl_handshake:
                    if self.ssl_handshake(sock) == SSL_HANDSHAKE_DONE:
                        # connection is ready for user IO
                        self.on_connect(self._conn_by_sock[sock])
                else:
                    self.handle_sock_io(fd, sock, mask)

    ##########################################################

    def loop_pass(self, poll_timeout):
        """
        :return: values returned by poll
        """
        fds = []
        try:
            fds = self.poller.poll(poll_timeout)
        except IOError as exc:
            if exc.errno != errno.EINTR:  # hibernate does that
                raise

        for fd, mask in fds:
            self.handle_fd_mask(fd, mask)
        self.deal_connects()
        return fds

    ##########################################################

    def deal_connects(self):
        now = time.time()
        to_remove = 0
        for when, address in self._plannned_connections:
            reconnect_interval = self._reconnect_intervals[address]
            if (when <= now) or (when > now + reconnect_interval * 2):
                to_remove += 1
                self._connect(address)
            else:
                break

        if to_remove:
            del self._plannned_connections[:to_remove]

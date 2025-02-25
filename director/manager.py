import jinja2
import logging
import os
import socket
import time
import uuid

import zmq
import zmq.auth
from zmq.auth.thread import ThreadAuthenticator

import director


class Interface(director.Processor):
    """The Interface class.

    This class defines everything required to connect to or from a given
    server.
    """

    def __init__(self, args):
        """Initialize the interface class.

        :param args: Arguments parsed by argparse.
        :type args: Object
        """

        super(Interface, self).__init__()

        self.args = args

        # Set log handlers to debug when enabled.
        if self.args.debug:
            self.log.setLevel(logging.DEBUG)
            for handler in self.log.handlers:
                handler.setLevel(logging.DEBUG)

        if self.args.mode == "client":
            self.bind_address = self.args.server_address
        elif self.args.mode == "server":
            self.bind_address = self.args.bind_address
        else:
            self.bind_address = "*"

        self.proto = "tcp"
        self.connection_string = "{proto}://{addr}".format(
            proto=self.proto, addr=self.bind_address
        )

        self.identity = socket.gethostname()

        self.heartbeat_liveness = 3
        self.heartbeat_interval = 1

        self.nullbyte = b"\000"  # Signals null
        self.heartbeat_ready = b"\001"  # Signals worker is ready
        self.heartbeat_notice = b"\005"  # Signals worker heartbeat
        self.job_ack = b"\006"  # Signals job started
        self.job_end = b"\004"  # Signals job ended
        self.job_processing = b"\026"  # Signals job running
        self.job_failed = b"\025"  # Signals job failed
        self.transfer_start = b"\002"  # Signals start file transfer
        self.transfer_end = b"\003"  # Signals start file transfer

        self.ctx = zmq.Context().instance()
        self.poller = zmq.Poller()

        self.template = jinja2.Environment(loader=jinja2.BaseLoader())

        self.base_dir = "/etc/director"
        self.public_keys_dir = os.path.join(self.base_dir, "public_keys")
        self.secret_keys_dir = os.path.join(self.base_dir, "private_keys")
        self.curve_keys_exist = os.path.exists(
            self.public_keys_dir
        ) and os.path.exists(self.secret_keys_dir)

    @property
    def get_heartbeat(self):
        """Return a new hearbeat interval time.

        :returns: Float
        """

        return time.time() + self.args.heartbeat_interval

    @property
    def get_expiry(self):
        """Return a new expiry time.

        :returns: Float
        """

        return time.time() + (
            self.heartbeat_interval * self.heartbeat_liveness
        )

    @property
    def get_uuid(self):
        """Return a new UUID in String format.

        :returns: String
        """

        return str(uuid.uuid4())

    def socket_bind(
        self, socket_type, connection, port, poller_type=zmq.POLLIN
    ):
        """Return a socket object which has been bound to a given address.

        When the socket_type is not PUB or PUSH, the bound socket will also be
        registered with self.poller as defined within the Interface class.

        :param socket_type: Set the Socket type, typically defined using a ZMQ
                            constant.
        :type socket_type: Integer
        :param connection: Set the Address information used for the bound
                           socket.
        :type connection: String
        :param port: Define the port which the socket will be bound to.
        :type port: Integer
        :param poller_type: Set the Socket type, typically defined using a ZMQ
                            constant.
        :type poller_type: Integer
        :returns: Object
        """

        bind = self.ctx.socket(socket_type)

        auth_enabled = (
            self.args.shared_key or self.args.curve_encryption
        ) or self.curve_keys_exist
        if auth_enabled:
            self.auth = ThreadAuthenticator(self.ctx, log=self.log)
            self.auth.start()
            self.auth.allow()
            if self.args.shared_key:
                # Enables basic auth
                self.auth.configure_plain(
                    domain="*", passwords={"admin": self.args.shared_key}
                )
                bind.plain_server = True  # Enable shared key authentication
                self.log.info("Shared key authentication enabled.")
            elif self.args.curve_encryption or self.curve_keys_exist:
                if not self.args.curve_encryption and self.curve_keys_exist:
                    self.log.info(
                        "Curve encryption enabled because key components are"
                        " on the system and no other authentication method"
                        " was defined."
                    )
                for item in [self.public_keys_dir, self.secret_keys_dir]:
                    if not os.path.exists(item):
                        raise SystemExit(
                            "The required path [ {} ] does not exist. Have"
                            " you generated your keys?".format(item)
                        )
                self.auth.configure_curve(
                    domain="*", location=self.public_keys_dir
                )
                server_secret_file = os.path.join(
                    self.secret_keys_dir, "server.key_secret"
                )
                server_public, server_secret = zmq.auth.load_certificate(
                    server_secret_file
                )
                bind.curve_secretkey = server_secret
                bind.curve_publickey = server_public
                bind.curve_server = True  # Enable curve authentication
        bind.bind(
            "{connection}:{port}".format(
                connection=connection,
                port=port,
            )
        )

        if socket_type not in [zmq.PUB]:
            self.poller.register(bind, poller_type)

        return bind

    def socket_connect(
        self,
        socket_type,
        connection,
        port,
        poller_type=zmq.POLLIN,
        send_ready=True,
    ):
        """Return a socket object which has been bound to a given address.

        When send_ready is set True and the socket_type is not SUB or PULL,
        the bound socket will send a single SOH ready message.

        :param socket_type: Set the Socket type, typically defined using a ZMQ
                            constant.
        :type socket_type: Integer
        :param connection: Set the Address information used for the bound
                           socket.
        :type connection: String
        :param port: Define the port which the socket will be bound to.
        :type port: Integer
        :param poller_type: Set the Socket type, typically defined using a ZMQ
                            constant.
        :type poller_type: Integer
        :param poller_type: Set the Socket type, typically defined using a ZMQ
                            constant.
        :type poller_type: Integer
        :returns: Object
        """

        bind = self.ctx.socket(socket_type)

        if self.args.shared_key:
            bind.plain_username = b"admin"  # User is hard coded.
            bind.plain_password = self.args.shared_key.encode()
            self.log.info("Shared key authentication enabled.")
        elif self.args.curve_encryption or self.curve_keys_exist:
            if not self.args.curve_encryption and self.curve_keys_exist:
                self.log.info(
                    "Curve encryption enabled because key components are"
                    " on the system and no other authentication method"
                    " was defined."
                )
            client_secret_file = os.path.join(
                self.secret_keys_dir, "client.key_secret"
            )
            for item in [self.public_keys_dir, self.secret_keys_dir]:
                if not os.path.exists(item):
                    raise SystemExit(
                        "The required path [ {} ] does not exist. Have"
                        " you generated your keys?".format(item)
                    )
            client_public, client_secret = zmq.auth.load_certificate(
                client_secret_file
            )
            bind.curve_secretkey = client_secret
            bind.curve_publickey = client_public
            server_public_file = os.path.join(
                self.public_keys_dir, "server.key"
            )
            server_public, _ = zmq.auth.load_certificate(server_public_file)
            bind.curve_serverkey = server_public

        if socket_type == zmq.SUB:
            bind.setsockopt_string(zmq.SUBSCRIBE, self.identity)
        else:
            bind.setsockopt_string(zmq.IDENTITY, self.identity)

        bind.linger = 0
        self.poller.register(bind, poller_type)
        bind.connect(
            "{connection}:{port}".format(
                connection=connection,
                port=port,
            )
        )

        if send_ready and socket_type not in [zmq.SUB, zmq.PULL]:
            self.socket_multipart_send(
                zsocket=bind, control=self.heartbeat_ready
            )

        return bind

    def run_threads(self, threads):
        """Execute process objects from an array.

        The array of threads are processed and started in a "daemon" mode.
        Once started the thread object is added into a cleanup array which
        is then joined.

        :param threads: An array of Process objects.
        :type threads: List
        """

        cleanup_threads = list()
        for t in threads:
            t.daemon = True
            t.start()
            cleanup_threads.append(t)

        for t in cleanup_threads:
            t.join()

    def socket_multipart_send(
        self,
        zsocket,
        identity=None,
        msg_id=None,
        control=None,
        command=None,
        data=None,
        info=None,
    ):
        """Send a message over a ZM0 socket.

        The message specification for server is as follows.

            [
                b"Identity"
                b"ID",
                b"ASCII Control Characters",
                b"command",
                b"data",
                b"info"
            ]

        The message specification for client is as follows.

            [
                b"ID",
                b"ASCII Control Characters",
                b"command",
                b"data",
                b"info"
            ]

        All message information is assumed to be byte encoded.

        All possible control characters are defined within the Interface class.
        For more on control characters review the following
        URL(https://donsnotes.com/tech/charsets/ascii.html#cntrl).

        :param zsocket: ZeroMQ socket object.
        :type zsocket: Object
        :param identity: Target where message will be sent.
        :type identity: Bytes
        :param msg_id: ID information for a given message. If no ID is
                       provided a UUID will be generated.
        :type msg_id: Bytes
        :param control: ASCII control charaters.
        :type control: Bytes
        :param command: Command definition for a given message.
        :type command: Bytes
        :param data: Encoded data that will be transmitted.
        :type data: Bytes
        :param info: Encoded information that will be transmitted.
        :type info: Bytes
        """

        if not msg_id:
            msg_id = self.get_uuid.encode()

        if not control:
            control = self.nullbyte

        if not command:
            command = self.nullbyte

        if not data:
            data = self.nullbyte

        if not info:
            info = self.nullbyte

        message_parts = [msg_id, control, command, data, info]

        if identity:
            message_parts.insert(0, identity)

        return zsocket.send_multipart(message_parts)

    @staticmethod
    def socket_multipart_recv(zsocket):
        """Receive a message over a ZM0 socket.

        The message specification for server is as follows.

            [
                b"Identity"
                b"ID",
                b"ASCII Control Characters",
                b"command",
                b"data",
                b"info"
            ]

        The message specification for client is as follows.

            [
                b"ID",
                b"ASCII Control Characters",
                b"command",
                b"data",
                b"info"
            ]

        All message parts are byte encoded.

        All possible control characters are defined within the Interface class.
        For more on control characters review the following
        URL(https://donsnotes.com/tech/charsets/ascii.html#cntrl).

        :param zsocket: ZeroMQ socket object.
        :type zsocket: Object
        """

        return zsocket.recv_multipart()

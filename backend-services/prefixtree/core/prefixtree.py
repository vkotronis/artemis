import multiprocessing as mp
import os
from typing import Dict
from typing import List
from typing import NoReturn

import pytricia
import requests
import ujson as json
from artemis_utils import flatten
from artemis_utils import get_ip_version
from artemis_utils import get_logger
from artemis_utils import RABBITMQ_URI
from artemis_utils import search_worst_prefix
from artemis_utils import translate_asn_range
from artemis_utils import translate_rfc2622
from artemis_utils.rabbitmq_util import create_exchange
from artemis_utils.rabbitmq_util import create_queue
from kombu import Connection
from kombu import Consumer
from kombu import Producer
from kombu import serialization
from kombu.mixins import ConsumerProducerMixin
from tornado.ioloop import IOLoop
from tornado.web import Application
from tornado.web import RequestHandler

# logger
log = get_logger()

# additional serializer for pg-amqp messages
serialization.register(
    "txtjson", json.dumps, json.loads, content_type="text", content_encoding="utf-8"
)

# shared memory object locks
shared_memory_locks = {
    "data_worker": mp.Lock(),
    "prefix_tree": mp.Lock(),
    "monitors": mp.Lock(),
    "monitored_prefixes": mp.Lock(),
    "configured_prefix_count": mp.Lock(),
    "config_timestamp": mp.Lock(),
}

# global vars
MODULE_NAME = os.getenv("MODULE_NAME", "prefixtree")
CONFIGURATION_HOST = os.getenv("CONFIGURATION_HOST", "configuration")
REST_PORT = int(os.getenv("REST_PORT", 3000))


# TODO: move this to artemis-utils
def pytricia_to_dict(pyt_tree):
    pyt_dict = {}
    for prefix in pyt_tree:
        pyt_dict[prefix] = pyt_tree[prefix]
    return pyt_dict


# TODO: move this to artemis-utils
def dict_to_pytricia(dict_tree, size=32):
    pyt_tree = pytricia.PyTricia(size)
    for prefix in dict_tree:
        pyt_tree.insert(prefix, dict_tree[prefix])
    return pyt_tree


def configure_prefixtree(msg, shared_memory_manager_dict):
    config = msg
    try:
        # check newer config
        shared_memory_locks["config_timestamp"].acquire()
        config_timestamp = shared_memory_manager_dict["config_timestamp"]
        shared_memory_locks["config_timestamp"].release()
        if config["timestamp"] > config_timestamp:

            # extract monitors
            monitors = msg.get("monitors", {})

            # calculate prefix tree
            prefix_tree = {"v4": pytricia.PyTricia(32), "v6": pytricia.PyTricia(128)}
            rules = config.get("rules", [])
            for rule in rules:
                rule_translated_origin_asn_set = set()
                for asn in rule["origin_asns"]:
                    this_translated_asn_list = flatten(translate_asn_range(asn))
                    rule_translated_origin_asn_set.update(set(this_translated_asn_list))
                rule["origin_asns"] = list(rule_translated_origin_asn_set)
                rule_translated_neighbor_set = set()
                for asn in rule["neighbors"]:
                    this_translated_asn_list = flatten(translate_asn_range(asn))
                    rule_translated_neighbor_set.update(set(this_translated_asn_list))
                rule["neighbors"] = list(rule_translated_neighbor_set)

                conf_obj = {
                    "origin_asns": rule["origin_asns"],
                    "neighbors": rule["neighbors"],
                    "prepend_seq": rule.get("prepend_seq", []),
                    "policies": set(rule.get("policies", [])),
                    "community_annotations": rule.get("community_annotations", []),
                    "mitigation": rule.get("mitigation", "manual"),
                }
                for prefix in rule["prefixes"]:
                    for translated_prefix in translate_rfc2622(prefix):
                        ip_version = get_ip_version(translated_prefix)
                        if prefix_tree[ip_version].has_key(translated_prefix):
                            node = prefix_tree[ip_version][translated_prefix]
                        else:
                            node = {
                                "prefix": translated_prefix,
                                "data": {"confs": []},
                                "timestamp": config["timestamp"],
                            }
                            prefix_tree[ip_version].insert(translated_prefix, node)
                        node["data"]["confs"].append(conf_obj)

            # calculate the monitored and configured prefixes
            configured_prefix_count = 0
            monitored_prefixes = set()
            for ip_version in prefix_tree:
                for prefix in prefix_tree[ip_version]:
                    configured_prefix_count += 1
                    monitored_prefix = search_worst_prefix(
                        prefix, prefix_tree[ip_version]
                    )
                    if monitored_prefix:
                        monitored_prefixes.add(monitored_prefix)

            shared_memory_locks["prefix_tree"].acquire()
            # note that the object should be picklable (e.g., dict instead of pytricia tree,
            # see also: https://github.com/jsommers/pytricia/issues/20)
            dict_prefix_tree = {
                "v4": pytricia_to_dict(prefix_tree["v4"]),
                "v6": pytricia_to_dict(prefix_tree["v6"]),
            }
            shared_memory_manager_dict["prefix_tree"] = dict_prefix_tree
            shared_memory_manager_dict["prefix_tree_recalculate"] = True
            shared_memory_locks["prefix_tree"].release()

            shared_memory_locks["monitors"].acquire()
            shared_memory_manager_dict["monitors"] = monitors
            shared_memory_locks["monitors"].release()

            shared_memory_locks["monitored_prefixes"].acquire()
            shared_memory_manager_dict["monitored_prefixes"] = monitored_prefixes
            shared_memory_locks["monitored_prefixes"].release()

            shared_memory_locks["configured_prefix_count"].acquire()
            shared_memory_manager_dict[
                "configured_prefix_count"
            ] = configured_prefix_count
            shared_memory_locks["configured_prefix_count"].release()

            shared_memory_locks["config_timestamp"].acquire()
            shared_memory_manager_dict["config_timestamp"] = config_timestamp
            shared_memory_locks["config_timestamp"].release()

            return {"success": True, "message": "configured"}
    except Exception:
        log.exception("exception")
        return {"success": False, "message": "error during data_task configuration"}


class ConfigHandler(RequestHandler):
    """
    REST request handler for configuration.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def post(self):
        """
        Cofnigures prefix tree and responds with a success message.
        :return: {"success": True | False, "message": < message >}
        """
        try:
            msg = json.loads(self.request.body)
            self.write(configure_prefixtree(msg, self.shared_memory_manager_dict))
        except Exception:
            self.write(
                {"success": False, "message": "error during data_task configuration"}
            )


class HealthHandler(RequestHandler):
    """
    REST request handler for health checks.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def get(self):
        """
        Extract the status of a service via a GET request.
        :return: {"status" : <unconfigured|running|stopped>}
        """
        status = "stopped"
        shared_memory_locks["data_worker"].acquire()
        if self.shared_memory_manager_dict["data_worker_running"]:
            status = "running"
        shared_memory_locks["data_worker"].release()
        self.write({"status": status})


class ControlHandler(RequestHandler):
    """
    REST request handler for control commands.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def start_data_worker(self):
        shared_memory_locks["data_worker"].acquire()
        if self.shared_memory_manager_dict["data_worker_running"]:
            log.info("data worker already running")
            shared_memory_locks["data_worker"].release()
            return "already running"
        shared_memory_locks["data_worker"].release()
        mp.Process(target=self.run_data_worker_process).start()
        return "instructed to start"

    def run_data_worker_process(self):
        try:
            with Connection(RABBITMQ_URI) as connection:
                shared_memory_locks["data_worker"].acquire()
                data_worker = PrefixTreeDataWorker(
                    connection, self.shared_memory_manager_dict
                )
                self.shared_memory_manager_dict["data_worker_running"] = True
                shared_memory_locks["data_worker"].release()
                log.info("data worker started")
                data_worker.run()
        except Exception:
            log.exception("exception")
        finally:
            shared_memory_locks["data_worker"].acquire()
            self.shared_memory_manager_dict["data_worker_running"] = False
            shared_memory_locks["data_worker"].release()
            log.info("data worker stopped")

    @staticmethod
    def stop_data_worker():
        shared_memory_locks["data_worker"].acquire()
        with Connection(RABBITMQ_URI) as connection:
            with Producer(connection) as producer:
                command_exchange = create_exchange("command", connection)
                producer.publish(
                    "",
                    exchange=command_exchange,
                    routing_key="stop-{}".format(MODULE_NAME),
                    serializer="ujson",
                )
        shared_memory_locks["data_worker"].release()
        message = "instructed to stop"
        return message

    def post(self):
        """
        Instruct a service to start or stop by posting a command.
        Sample request body
        {
            "command": <start|stop>
        }
        :return: {"success": True|False, "message": <message>}
        """
        try:
            msg = json.loads(self.request.body)
            command = msg["command"]
            # start/stop data_worker
            if command == "start":
                message = self.start_data_worker()
                self.write({"success": True, "message": message})
            elif command == "stop":
                message = self.stop_data_worker()
                self.write({"success": True, "message": message})
            else:
                self.write({"success": False, "message": "unknown command"})
        except Exception:
            log.exception("Exception")
            self.write({"success": False, "message": "error during control"})


class MonitorHandler(RequestHandler):
    """
    REST request handler for monitor information.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def get(self):
        """
        Simply provides the configured monitors (in the form of a JSON dict) to the requester
        """
        shared_memory_locks["monitors"].acquire()
        self.write({"monitors": self.shared_memory_manager_dict["monitors"]})
        shared_memory_locks["monitors"].release()


class ConfiguredPrefixCountHandler(RequestHandler):
    """
    REST request handler for configured prefix count information.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def get(self):
        """
        Simply provides the configured prefix count (in the form of a JSON dict) to the requester
        """
        shared_memory_locks["configured_prefix_count"].acquire()
        self.write(
            {
                "configured_prefix_count": self.shared_memory_manager_dict[
                    "configured_prefix_count"
                ]
            }
        )
        shared_memory_locks["configured_prefix_count"].release()


class MonitoredPrefixesHandler(RequestHandler):
    """
    REST request handler for  monitored prefixes information.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def get(self):
        """
        Simply provides the monitored prefixes (in the form of a JSON dict) to the requester
        """
        shared_memory_locks["monitored_prefixes"].acquire()
        self.write(
            {
                "monitored_prefixes": list(
                    self.shared_memory_manager_dict["monitored_prefixes"]
                )
            }
        )
        shared_memory_locks["monitored_prefixes"].release()


class PrefixTree:
    """
    Prefix Tree Service.
    """

    def __init__(self):
        # initialize shared memory
        shared_memory_manager = mp.Manager()
        self.shared_memory_manager_dict = shared_memory_manager.dict()
        self.shared_memory_manager_dict["data_worker_running"] = False
        self.shared_memory_manager_dict["prefix_tree"] = {"v4": {}, "v6": {}}
        self.shared_memory_manager_dict["prefix_tree_recalculate"] = True
        self.shared_memory_manager_dict["monitors"] = {}
        self.shared_memory_manager_dict["monitored_prefixes"] = set()
        self.shared_memory_manager_dict["configured_prefix_count"] = 0
        self.shared_memory_manager_dict["config_timestamp"] = -1

        log.info("service initiated")

    def make_rest_app(self):
        return Application(
            [
                (
                    "/config",
                    ConfigHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/control",
                    ControlHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/health",
                    HealthHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/monitors",
                    MonitorHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/configuredPrefixCount",
                    ConfiguredPrefixCountHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/monitoredPrefixes",
                    MonitoredPrefixesHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
            ]
        )

    def start_rest_app(self):
        app = self.make_rest_app()
        app.listen(REST_PORT)
        log.info("REST worker started and listening to port {}".format(REST_PORT))
        IOLoop.current().start()


class PrefixTreeDataWorker(ConsumerProducerMixin):
    """
    RabbitMQ Consumer/Producer for the prefix tree Service.
    """

    def __init__(
        self, connection: Connection, shared_memory_manager_dict: Dict
    ) -> NoReturn:
        self.connection = connection
        self.shared_memory_manager_dict = shared_memory_manager_dict
        self.prefix_tree = {"v4": pytricia.PyTricia(32), "v6": pytricia.PyTricia(128)}
        shared_memory_locks["prefix_tree"].acquire()
        if self.shared_memory_manager_dict["prefix_tree_recalculate"]:
            for ip_version in ["v4", "v6"]:
                if ip_version == "v4":
                    size = 32
                else:
                    size = 128
                self.prefix_tree[ip_version] = dict_to_pytricia(
                    self.shared_memory_manager_dict["prefix_tree"][ip_version], size
                )
                log.info(
                    "{} pytricia tree parsed from configuration".format(ip_version)
                )
                self.shared_memory_manager_dict["prefix_tree_recalculate"] = False
        shared_memory_locks["prefix_tree"].release()

        # EXCHANGES
        self.update_exchange = create_exchange("bgp-update", connection, declare=True)
        self.hijack_exchange = create_exchange(
            "hijack-update", connection, declare=True
        )
        self.pg_amq_bridge = create_exchange("amq.direct", connection)
        self.mitigation_exchange = create_exchange(
            "mitigation", connection, declare=True
        )
        self.command_exchange = create_exchange("command", connection, declare=True)

        # QUEUES
        self.update_queue = create_queue(
            MODULE_NAME, exchange=self.update_exchange, routing_key="update", priority=1
        )
        self.hijack_ongoing_queue = create_queue(
            MODULE_NAME,
            exchange=self.hijack_exchange,
            routing_key="ongoing",
            priority=1,
        )
        self.pg_amq_update_queue = create_queue(
            MODULE_NAME,
            exchange=self.pg_amq_bridge,
            routing_key="update-insert",
            priority=1,
        )
        self.mitigation_request_queue = create_queue(
            MODULE_NAME,
            exchange=self.mitigation_exchange,
            routing_key="mitigate",
            priority=2,
        )
        self.stop_queue = create_queue(
            MODULE_NAME,
            exchange=self.command_exchange,
            routing_key="stop-{}".format(MODULE_NAME),
            priority=1,
        )

        log.info("data worker initiated")

    def get_consumers(self, Consumer: Consumer, channel: Connection) -> List[Consumer]:
        return [
            Consumer(
                queues=[self.update_queue],
                on_message=self.annotate_bgp_update,
                prefetch_count=100,
                accept=["ujson"],
            ),
            Consumer(
                queues=[self.hijack_ongoing_queue],
                on_message=self.annotate_ongoing_hijack_updates,
                prefetch_count=100,
                accept=["ujson"],
            ),
            Consumer(
                queues=[self.mitigation_request_queue],
                on_message=self.annotate_mitigation_request,
                prefetch_count=100,
                accept=["ujson"],
            ),
            Consumer(
                queues=[self.pg_amq_update_queue],
                on_message=self.annotate_stored_bgp_update,
                prefetch_count=100,
                accept=["ujson", "txtjson"],
            ),
            Consumer(
                queues=[self.stop_queue],
                on_message=self.stop_consumer_loop,
                prefetch_count=100,
                accept=["ujson"],
            ),
        ]

    def find_prefix_node(self, prefix):
        ip_version = get_ip_version(prefix)
        prefix_node = None
        shared_memory_locks["prefix_tree"].acquire()
        if ip_version == "v4":
            size = 32
        else:
            size = 128
        # need to turn to pytricia tree since this means that the tree has changed due to re-configuration
        if self.shared_memory_manager_dict["prefix_tree_recalculate"]:
            self.prefix_tree[ip_version] = dict_to_pytricia(
                self.shared_memory_manager_dict["prefix_tree"][ip_version], size
            )
            log.info("{} pytricia tree re-parsed from configuration".format(ip_version))
            self.shared_memory_manager_dict["prefix_tree_recalculate"] = False
        if prefix in self.prefix_tree[ip_version]:
            prefix_node = self.prefix_tree[ip_version][prefix]
        shared_memory_locks["prefix_tree"].release()
        return prefix_node

    def annotate_bgp_update(self, message: Dict) -> NoReturn:
        """
        Callback function that annotates an incoming bgp update with the associated
        configuration node (otherwise it discards it).
        """
        message.ack()
        bgp_update = message.payload
        try:
            prefix_node = self.find_prefix_node(bgp_update["prefix"])
            if prefix_node:
                bgp_update["prefix_node"] = prefix_node
                self.producer.publish(
                    bgp_update,
                    exchange=self.update_exchange,
                    routing_key="update-with-prefix-node",
                    serializer="ujson",
                )
            else:
                # log.error("unconfigured BGP update received '{}'".format(bgp_update))
                pass
        except Exception:
            log.exception("exception")

    def annotate_stored_bgp_update(self, message: Dict) -> NoReturn:
        """
        Callback function that annotates an incoming (stored) bgp update with the associated
        configuration node (otherwise it discards it).
        """
        message.ack()
        bgp_update = message.payload
        try:
            prefix_node = self.find_prefix_node(bgp_update["prefix"])
            if prefix_node:
                bgp_update["prefix_node"] = prefix_node
                self.producer.publish(
                    bgp_update,
                    exchange=self.update_exchange,
                    routing_key="stored-update-with-prefix-node",
                    serializer="ujson",
                )
            else:
                # log.error(
                #     "unconfigured stored BGP update received '{}'".format(bgp_update)
                # )
                pass
        except Exception:
            log.exception("exception")

    def annotate_ongoing_hijack_updates(self, message: Dict) -> NoReturn:
        """
        Callback function that annotates incoming ongoing hijack updates with the associated
        configuration nodes (otherwise it discards them).
        """
        message.ack()
        bgp_updates = []
        for bgp_update in message.payload:
            try:
                prefix_node = self.find_prefix_node(bgp_update["prefix"])
                if prefix_node:
                    bgp_update["prefix_node"] = prefix_node
                bgp_updates.append(bgp_update)
            except Exception:
                log.exception("exception")
        self.producer.publish(
            bgp_updates,
            exchange=self.hijack_exchange,
            routing_key="ongoing-with-prefix-node",
            serializer="ujson",
        )

    def annotate_mitigation_request(self, message: Dict) -> NoReturn:
        """
        Callback function that annotates incoming hijack mitigation requests with the associated
        mitigation action/instruction (otherwise it discards them).
        """
        message.ack()
        mit_request = message.payload
        try:
            prefix_node = self.find_prefix_node(mit_request["prefix"])
            if prefix_node:
                annotated_mit_request = {}
                # use the first best matching rule mitigation action;
                # a prefix should not have different mitigation actions anyway
                annotated_mit_request["hijack_info"] = mit_request
                annotated_mit_request["mitigation_action"] = prefix_node["data"][
                    "confs"
                ][0]["mitigation"]
                self.producer.publish(
                    annotated_mit_request,
                    exchange=self.mitigation_exchange,
                    routing_key="mitigate-with-action",
                    serializer="ujson",
                )
        except Exception:
            log.exception("exception")

    def stop_consumer_loop(self, message: Dict) -> NoReturn:
        """
        Callback function that stop the current consumer loop
        """
        message.ack()
        self.should_stop = True


if __name__ == "__main__":
    # initiate prefix tree service with REST
    prefixTreeService = PrefixTree()

    # try to get configuration upon start (it is OK if it fails, will get it from POST)
    # (this is needed because service may restart while configuration is running)
    try:
        r = requests.get("http://{}:{}/config".format(CONFIGURATION_HOST, REST_PORT))
        conf_res = configure_prefixtree(
            r.json(), prefixTreeService.shared_memory_manager_dict
        )
        if not conf_res["success"]:
            log.info(
                "could not get configuration upon startup, will get via POST later"
            )
    except Exception:
        log.info("could not get configuration upon startup, will get via POST later")

    # start REST within main process
    prefixTreeService.start_rest_app()

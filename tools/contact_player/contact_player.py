#!/usr/bin/env python3


from tc_netem import *
from ccp import *
import argparse
import time
import signal
import sys
import os
import yaml
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Iterable


@dataclass
class NetworkInterface:
    ip: str
    dev: str = ""

@dataclass
class Node:
    name: str
    eid: str
    interfaces: Dict[str, NetworkInterface] = field(default_factory=dict)

@dataclass(frozen=True)
class Link:
    node1: str
    node2: str
    link_type: str = "-"

    @classmethod
    def create(cls, n1: str, n2: str, link_type: str = "-") -> "Link":
        sorted_nodes = sorted([n1, n2])
        return cls(sorted_nodes[0], sorted_nodes[1], link_type)


def load_scenario(path: str) -> List[Node]:
    """
    Loads the docker compose scenario from the passed filepath.
    """
    print(f"Loading scenario from {path}.")
    nodes: List[Node] = []

    with open(path) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
        if "x-description" in config:
            print(f"Description: {config['x-description']}")

        services = config.get("services", {})
        for name, item in services.items():
            env_vars: list[str] = item.get("environment", [])
            node_id_env = next((var for var in env_vars if var.startswith("NODE_ID=")), None)
            if not node_id_env:
                continue

            node_eID = f"ipn:{node_id_env.split('=')[1]}.0"
            node = Node(name=name, eid=node_eID)

            for net_name, value in item.get("networks", {}).items():
                node.interfaces[net_name] = NetworkInterface(ip=value["ipv4_address"])
                print(
                    f"Node {node_eID} connected to network {net_name} with {value['ipv4_address']}"
                )

            nodes.append(node)

    print(f"Created {len(nodes)} nodes.")
    return nodes


def find_common_subnet_between_nodes(node1: Node, node2: Node) -> Optional[str]:
    for net in node1.interfaces.keys():
        if net in node2.interfaces:
            return net
    return None


def get_dev_for_subnet(node: Node, subnet: str) -> str:
    return node.interfaces[subnet].dev


def get_network_for_interface(node: Node, interface: str) -> Optional[str]:
    for net_name, net_if in node.interfaces.items():
        if net_if.dev == interface:
            return net_name
    print(f"WARNING: could not find a network for interface {interface} on node {node.name}!")
    return None


def contact_to_node_iface(
    contact: CoreContact, nodes: Dict[str, Node]
) -> List[Tuple[str, str]]:
    """
    Resolve a contact into the list of (node, interface) tuples, taking (a)symmetry of the contact into account.
    """
    node1_name = contact.nodes[0]
    node2_name = contact.nodes[1]

    node_iface_tuples: List[Tuple[str, str]] = []

    if node2_name.startswith("dev:"):
        node1_iface = node2_name.split(":")[1] + "_0"
        node_iface_tuples.append((node1_name, node1_iface))

        if contact.symmetric:
            node1 = nodes[node1_name]
            network = get_network_for_interface(node1, node1_iface)
            if network is None:
                return node_iface_tuples

            node2_iface: Optional[str] = None
            for candidate_name, candidate_node in nodes.items():
                if candidate_name == node1_name:
                    continue

                if network in candidate_node.interfaces:
                    node2_iface = candidate_node.interfaces[network].dev
                    node2_name = candidate_name
                    break
            
            if node2_iface is None:
                print(f"WARNING: Could not apply symmetric rule for {node1_name} <-> {node2_name}")
                return node_iface_tuples
            node_iface_tuples.append((node2_name, node2_iface))

        return node_iface_tuples

    node1 = nodes[node1_name]
    node2 = nodes[node2_name]
    network = find_common_subnet_between_nodes(node1, node2)
    if network is None:
        print(f"WARNING: No common network between {node1_name} and {node2_name}")
        return []
    
    node1_iface = get_dev_for_subnet(node1, network)
    node_iface_tuples.append((node1_name, node1_iface))

    if contact.symmetric:
        node2_iface = get_dev_for_subnet(node2, network)
        node_iface_tuples.append((node2_name, node2_iface))

    return node_iface_tuples


def set_link(
    nodes: Dict[str, Node],
    contact: CoreContact,
    deactivate: bool = False,
    command: str = "change",
):
    loss = contact.loss
    if deactivate:
        loss = 100.0

    node_iface_tuples = contact_to_node_iface(contact, nodes)

    for node_name, iface in node_iface_tuples:
        set_on_interface(
            node_name,
            iface,
            command=command,
            loss=loss,
            delay=contact.delay,
            jitter=contact.jitter,
            bandwidth=contact.bw,
        )


def get_pure_node_links(links: Iterable[Link]) -> Set[Link]:
    pure_node_links = set()
    for l in links:
        n1 = l.node1
        n2 = l.node2
        orig_n1 = n1
        orig_n2 = n2
        
        if orig_n1.startswith("dev:"):
            dev_str = orig_n1.split(":")[1]
            components = dev_str.split("_")
            if len(components) >= 2:
                if components[0] == orig_n2:
                    n1 = components[1]
                elif components[1] == orig_n2:
                    n1 = components[0]
            else:
                print(f"Warning: Dev string {dev_str} not mappable to nodes, skipping link.")
                
        if orig_n2.startswith("dev:"):
            dev_str = orig_n2.split(":")[1]
            components = dev_str.split("_")
            if len(components) >= 2:
                if components[0] == orig_n1:
                    n2 = components[1]
                elif components[1] == orig_n1:
                    n2 = components[0]
            else:
                print(f"Warning: Dev string {dev_str} not mappable to nodes, skipping link.")
                
        pure_node_links.add(Link.create(n1, n2, l.link_type))
    return pure_node_links


def update_netmap(netmap: bool, scenario_name: str, links: Set[Link]):
    if netmap:
        if not os.path.exists("tmp"):
            os.makedirs("tmp")

        pure_node_links = get_pure_node_links(links)

        print(f"Updating network map tmp/{scenario_name}.netmap")
        with open(f"tmp/{scenario_name}.netmap", "w") as f:
            for l in pure_node_links:
                f.write(f"{l.node1} {l.link_type} {l.node2}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--loop", metavar="LOOP", type=bool, help="Override looping")
    parser.add_argument("-m", "--map-network", help="Map network links", action="store_true")
    parser.add_argument("scenario", help="scenario file to load")
    parser.add_argument("ccp", help="core contact plan to load")
    args = parser.parse_args()

    scenario_nodes = load_scenario(args.scenario)
    netmap = args.map_network

    mapping = {}
    nodes: Dict[str, Node] = {}

    for node in scenario_nodes:
        node_id = node.eid.split(":")[1].split(".")[0]
        mapping[node_id] = node.name
        
        for net_name, interface in node.interfaces.items():
            res = run_in_container(node.name, f"ip a | grep {interface.ip}")
            if len(res) == 0:
                print(f"Error: IP {interface.ip} not found in container {node.name}. This is likely a network configuration issue or container startup problem.")
                continue
            net_if = res.rsplit(" ", maxsplit=1)[1].strip()
            interface.dev = net_if
            
        nodes[node.name] = node

    links: Set[Link] = set()
    for n1_name, n1 in nodes.items():
        for n2_name, n2 in nodes.items():
            if n1_name == n2_name:
                continue
            link_subnet = find_common_subnet_between_nodes(n1, n2)
            if link_subnet is not None:
                links.add(Link.create(n1_name, n2_name, "-"))

    scenario_name = os.path.splitext(os.path.basename(args.scenario))[0]
    print(links)

    plan = CoreContactPlan.from_file(args.ccp, mapping=mapping)

    container_devs: List[Tuple[str, str]] = []
    for contact in plan.contacts:
        node_iface_tuples = contact_to_node_iface(contact, nodes)
        container_devs.extend(node_iface_tuples)

    container_devs = list(set(container_devs))

    all_contacts = plan.all_contacts()
    all_contacts_links: Set[Link] = set()
    
    for c in all_contacts:
        n1 = c[0]
        n2 = c[1]
        orig_n1 = n1
        orig_n2 = n2
        if orig_n1.startswith("dev:"):
            dev_str = orig_n1.split(":")[1]
            components = dev_str.split("_")
            if len(components) >= 2:
                if components[0] == orig_n2:
                    n1 = components[1]
                elif components[1] == orig_n2:
                    n1 = components[0]
            else:
                print(f"Warning: Dev string {dev_str} not mappable to nodes, skipping link.")
        if orig_n2.startswith("dev:"):
            dev_str = orig_n2.split(":")[1]
            components = dev_str.split("_")
            if len(components) >= 2:
                if components[0] == orig_n1:
                    n2 = components[1]
                elif components[1] == orig_n1:
                    n2 = components[0]
            else:
                print(f"Warning: Dev string {dev_str} not mappable to nodes, skipping link.")
        all_contacts_links.add(Link.create(n1, n2, "-"))

    print("all contacts sorted pairs: ", all_contacts_links)

    links = links - all_contacts_links
    print("links: ", links)

    update_netmap(netmap, scenario_name, links)

    def signal_handler(sig, frame):
        global args
        print("You pressed Ctrl+C")
        fixed = plan.fixed
        for contact in fixed:
            print("Deactivating fixed contact %s" % contact)
            set_link(nodes, contact, command="del")
        for c, d in container_devs:
            print(f"Removing tc netem for {c} on device {d}")
            set_on_interface(c, d, command="del", loss=0.0)

        sys.exit(0)

    for c, d in container_devs:
        print(f"Setting up tc for {c} on device {d} with 100% loss")
        set_on_interface(c, d, command="add", loss=100.0)

    signal.signal(signal.SIGINT, signal_handler)

    fixed = plan.fixed
    for contact in fixed:
        print("Activating fixed contact %s" % contact)
        set_link(nodes, contact, command="add")

    cur_time = 0

    control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control_socket.bind(("localhost", 9966))
    control_socket.setblocking(False)

    while True:
        if (
            plan.next_activation(cur_time) is None
            and plan.next_deactivation(cur_time) is None
        ):
            if plan.loop or args.loop:
                print("Looping")
                cur_time = 0
                plan.reset()
                continue
            else:
                print("No more events")
                break
        next_event = min(
            [
                t
                for t in [
                    plan.next_activation(cur_time),
                    plan.next_deactivation(cur_time),
                ]
                if t is not None
            ]
        )
        print("[ %d ] Next event(s) at %d" % (cur_time, next_event))
        sleep_time = next_event - cur_time
        time_slept = 0
        SLEEP_DELAY = 0.1
        paused = False
        while time_slept < sleep_time:
            try:
                received_data, addr = control_socket.recvfrom(1024)
                data = received_data.strip()
                print(f"Received control message: {data}")
                if data == b"resume" and paused:
                    paused = False
                    print("cmd: Resuming normal operation")
                    continue
                if data == b"pause" and not paused:
                    paused = True
                    print("cmd: Pausing, waiting for 'resume' message to continue")
                if data == b"next":
                    print("cmd: Skipping to next")
                    break
                if data == b"time":
                    print(f"cmd: Current time is {cur_time + time_slept}")
                    control_socket.sendto(
                        f"{cur_time + int(time_slept)} {next_event}".encode(), addr
                    )
                if data == b"scenario":
                    print(f"cmd: Current scenario is {args.scenario} with {args.ccp}")
                    response = f"{args.scenario} {args.ccp}"
                    control_socket.sendto(response.encode(), addr)

                if data == b"links":
                    pure_node_links = get_pure_node_links(links)
                    print(f"cmd: Current links are {pure_node_links}")
                    response = "\n".join(
                        [f"{l.node1} {l.link_type} {l.node2}" for l in pure_node_links]
                    )
                    control_socket.sendto(response.encode(), addr)

            except socket.error as e:
                pass

            if sleep_time - time_slept < 1:
                time.sleep(sleep_time - time_slept)
                break
            else:
                time.sleep(SLEEP_DELAY)
                if not paused:
                    time_slept += SLEEP_DELAY
        
        cur_time = next_event
        for contact, state in plan.need_activation(cur_time):
            print("[ %d ] Activating %s" % (cur_time, contact))
            set_link(nodes, contact)
            
            static_link = Link.create(contact.nodes[0], contact.nodes[1], "-")
            if static_link in links:
                links.remove(static_link)
            
            active_link = Link.create(contact.nodes[0], contact.nodes[1], ".")
            links.add(active_link)

            plan.contacts[contact] = ContactState.LIVE

        for contact, state in plan.need_deactivation(cur_time):
            print("[ %d ] Deactivating %s" % (cur_time, contact))
            set_link(nodes, contact, deactivate=True)
            
            active_link = Link.create(contact.nodes[0], contact.nodes[1], ".")
            if active_link in links:
                links.remove(active_link)
            
            plan.contacts[contact] = ContactState.POST

        update_netmap(netmap, scenario_name, links)

    for c, d in container_devs:
        print(f"Removing tc netem for {c} on device {d}")
        set_on_interface(c, d, command="del", loss=0.0)

    links.clear()
    update_netmap(netmap, scenario_name, links)


if __name__ == "__main__":
    main()

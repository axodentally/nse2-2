1. Import `dataclass` and define `NetworkInterface`, `Node`, `Link`.
2. Refactor `load_scenario` to return `Dict[str, Node]` mapped by node.name instead of nested dictionaries.
3. Update `find_common_subnet_between_nodes`, `get_dev_for_subnet`, `get_network_for_interface` to use `Node` instances.
4. Refactor `contact_to_node_iface` to use `Node` and `NetworkInterface`.
5. Update `get_pure_node_links` and `update_netmap` to handle `Link` objects instead of lists/tuples.
6. Clean up the `main` function loop that gathers contacts and modifies links.

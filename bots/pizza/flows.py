"""
Pizza bot conversation flows using pipecat-flows (dynamic flow mode).

Defines a 3-node dynamic flow:
1. select_size     — ask for pizza size
2. select_toppings — ask for toppings
3. confirm_order   — confirm the complete order

Learning note (M4):
    Dynamic flows in pipecat-flows work via NodeConfig TypedDicts:
    - task_messages: instructions for the LLM at this node
    - functions: FlowsFunctionSchema objects with handler= set

    Each handler returns a (FlowResult, next_node_config) tuple.
    FlowManager transitions to next_node_config automatically.

    The flow is "dynamic" because node configs can be generated at
    runtime (e.g., incorporating the pizza size into the next node's prompt).
"""

from pipecat_flows.types import FlowArgs, FlowsFunctionSchema, NodeConfig


def create_select_size_node(order: dict) -> NodeConfig:
    """First node: ask for pizza size."""

    async def handle_select_size(args: FlowArgs) -> tuple:
        # `order` is captured from the outer scope
        size = args["size"]
        order["size"] = size
        # Return (result, next_node) — FlowManager transitions to next_node
        return (
            {"status": "success", "size": size},
            create_select_toppings_node(size, order),
        )

    return {
        "name": "select_size",
        "task_messages": [
            {
                "role": "system",
                "content": (
                    "Greet the user and introduce yourself as Marco from Acme Pizza. "
                    "Then ask what size pizza they want: small, medium, or large. "
                    "Once they specify a size, call select_size with that size."
                ),
            }
        ],
        "functions": [
            FlowsFunctionSchema(
                name="select_size",
                description="Record the pizza size selected by the user",
                properties={
                    "size": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                        "description": "The pizza size",
                    }
                },
                required=["size"],
                # handler is a closure over `order` — FlowManager calls handler(args, flow_manager)
                # but we only need args here, so we use a 1-arg lambda wrapper
                handler=handle_select_size,
            )
        ],
    }


def create_select_toppings_node(size: str, order: dict) -> NodeConfig:
    """Second node: ask for toppings (size known)."""

    async def handle_select_toppings(args: FlowArgs) -> tuple:
        toppings = args.get("toppings", [])
        order["toppings"] = toppings
        return (
            {"status": "success", "toppings": toppings},
            create_confirm_order_node(order["size"], toppings, order),
        )

    return {
        "name": "select_toppings",
        "task_messages": [
            {
                "role": "system",
                "content": (
                    f"The user wants a {size} pizza. "
                    "Ask what toppings they'd like. Options: margherita, pepperoni, mushrooms, olives, peppers. "
                    "They can choose multiple. Once they say what they want, call select_toppings."
                ),
            }
        ],
        "functions": [
            FlowsFunctionSchema(
                name="select_toppings",
                description="Record the toppings selected by the user",
                properties={
                    "toppings": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of toppings",
                    }
                },
                required=["toppings"],
                handler=handle_select_toppings,
            )
        ],
    }


def create_confirm_order_node(size: str, toppings: list, order: dict) -> NodeConfig:
    """Third node: confirm and finalize the order."""
    toppings_str = ", ".join(toppings) if toppings else "no toppings"

    async def handle_confirm_order(args: FlowArgs) -> tuple:
        order["confirmed"] = True
        # None as next_node = flow ends (LLM says goodbye)
        return {"status": "success", "message": "Order placed successfully!"}, None

    async def handle_restart_order(args: FlowArgs) -> tuple:
        order.clear()
        return {"status": "success"}, create_select_size_node()

    return {
        "name": "confirm_order",
        "task_messages": [
            {
                "role": "system",
                "content": (
                    f"The user wants a {size} pizza with {toppings_str}. "
                    "Read back the complete order and ask the user to confirm. "
                    "If they say yes, call confirm_order. "
                    "If they want to change something, call restart_order."
                ),
            }
        ],
        "functions": [
            FlowsFunctionSchema(
                name="confirm_order",
                description="Finalize and place the order",
                properties={},
                required=[],
                handler=handle_confirm_order,
            ),
            FlowsFunctionSchema(
                name="restart_order",
                description="Start the order over from the beginning",
                properties={},
                required=[],
                handler=handle_restart_order,
            ),
        ],
    }

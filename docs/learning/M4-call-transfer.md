# M4 Learning Guide — Multi-Bot Call Transfer

## The Stem (growing with M4)

```python
# Tech support pipeline (same as M3 + function tool)
pipeline_ts = Pipeline([transport.input(), stt, user_agg_ts, llm_ts, tts_nova, transport.output(), asst_agg_ts])
task_ts = PipelineTask(pipeline_ts, ...)
# On transfer:
await task_ts.cancel()

# Pizza pipeline (new task, same transport)
pipeline_pizza = Pipeline([transport.input(), stt, user_agg_pizza, llm_pizza, tts_shimmer, transport.output(), asst_agg_pizza])
task_pizza = PipelineTask(pipeline_pizza, ...)
flow_manager = FlowManager(task=task_pizza, llm=llm_pizza, context_aggregator=user_agg_pizza)
await flow_manager.initialize(create_select_size_node(order))
```

**New in M4:** Two pipelines sequenced on the same transport. Function calling drives the switch.

---

## Example 1 — Tech Support Normal Turn

**Do:** Say "My laptop won't boot"

**Observe in logs:**
```
[TECH-SUPPORT] Starting tech support bot
[METRICS] TTFB | processor=OpenAISTTService value=0.41s
[METRICS] TTFB | processor=OpenAILLMService value=0.73s
[METRICS] TTFB | processor=OpenAITTSService value=0.29s
[METRICS] LLM tokens | prompt=142 completion=89 total=231
```

**Understand:**
Identical to M3. The function tool (`transfer_to_pizza`) is registered with the LLM via `ToolsSchema` in the `LLMContext`, but the LLM only calls it when it judges the request is relevant. For a tech support question, the LLM ignores the tool and responds normally.

The tool is there — the LLM just doesn't call it. This is function calling's key property: **the model decides when to use tools**, not the application.

---

## Example 2 — Trigger Call Transfer

**Do:** Say "I'd like to order a pizza" or "Can you help me order some food?"

**Observe in logs:**
```
[TECH-SUPPORT] Client connected — queuing greeting
[METRICS] TTFB | processor=OpenAILLMService value=0.65s    ← generates: "Transferring you..."
transfer_to_pizza() called — initiating handoff
[TRANSFER] Context summary: This user was just transferred...
[TRANSFER] Starting pizza bot pipeline
```

**Hear:** Voice switches from nova (Alex) to shimmer (Marco) mid-call.

**Understand:**
1. LLM detects pizza intent → generates `FunctionCallInProgressFrame` for `transfer_to_pizza`
2. Handler runs: calls `result_callback` (which triggers "Transferring..." TTS response), then `await task.cancel()`
3. `CancelFrame` flows downstream → all processors cleanup
4. `run_pizza_bot(transport, messages)` starts on the same transport — no WebRTC interruption
5. Pizza bot inherits the transport's media tracks; ICE negotiation already done

The voice change is the audible confirmation that the pipeline changed.

---

## Example 3 — Pizza Bot Multi-Turn with State

**Do:** After transfer, order "a large pizza with pepperoni and mushrooms, then confirm"

**Observe:** Flow transitions visible in logs:
```
[PIZZA] Starting pizza bot pipeline
FlowManager initialized in dynamic mode
← User: "large"
select_size() called with {'size': 'large'}
→ Transitioning to: select_toppings node
← User: "pepperoni and mushrooms"
select_toppings() called with {'toppings': ['pepperoni', 'mushrooms']}
→ Transitioning to: confirm_order node
← User: "yes, confirm"
confirm_order() called
→ next_node=None (flow ends)
```

**Understand:**
`FlowManager` manages state as a graph of `NodeConfig` objects. Each node has:
- `task_messages`: system prompt for this state
- `functions`: FlowsFunctionSchema objects with handlers

When a handler returns `(result, next_node_config)`, FlowManager:
1. Calls `result_callback(result, properties=FunctionCallResultProperties(run_llm=False))`
2. After context is updated, transitions to `next_node_config`
3. Updates LLM tools to next node's functions
4. Triggers `LLMRunFrame` so the LLM responds in the new state

This is the state machine pattern applied to conversation: each state knows what it needs from the user and what comes next.

---

## Example 4 — Interrupt During Transfer Handoff

**Do:** Say "transfer me to pizza" then immediately say "wait, never mind" while the bot is saying "Transferring..."

**Observe:** Transfer still completes. Pizza bot starts. Your "wait, never mind" becomes the first message to the pizza bot.

**Understand:**
`result_callback` is called inside the function handler with `run_llm=False`. This wraps the result in an `UninterruptibleFrame`, which bypasses the interruption system.

The function call result MUST be written to context to keep the conversation coherent. Even if the user interrupts, the `FunctionCallResultFrame` survives. The transfer still executes.

Your "wait, never mind" is then processed as new user input by the pizza bot — which will politely continue taking the order.

This is why important state changes (like function call results) are `UninterruptibleFrame`: consistency matters more than speed here.

---

## Example 5 — Inspect Context at Handoff

**Do:** Add `logger.info(f"Tech support messages at transfer: {messages}")` before `run_pizza_bot()` in `transfer_server.py`. Trigger a transfer after one tech support turn.

**Observe:**
```python
[
  {"role": "system", "content": "You are Alex..."},
  {"role": "system", "content": "Greet the user: Hi, you've reached..."},
  {"role": "assistant", "content": "Hi! You've reached Acme Tech Support..."},
  {"role": "user", "content": "I'd like to order a pizza"},
  {"role": "assistant", "content": "Transferring you now...",
   "tool_calls": [{"function": {"name": "transfer_to_pizza"}}]},
  {"role": "tool", "content": '{"status": "transferring"}', "tool_call_id": "..."}
]
```

Pizza bot receives a summary system message, not this full list:
```
"This user was just transferred from tech support. They mentioned: 'I'd like to order a pizza'. They now want to order a pizza."
```

**Understand:**
The summary extracts signal from the conversation without the token cost of the full history. The pizza bot doesn't need to know about the tool call result or the system prompts — it just needs: "user was transferred, they mentioned pizza."

Full context hand-off vs summary is a trade-off:
- **Full context**: pizza bot has complete conversation history, but more tokens, more context pollution
- **Summary**: cheaper, focused, but lossy (fine for most use cases)

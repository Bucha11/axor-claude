"""A fake Anthropic client: just enough of messages.stream / messages.create."""
import json, re
from types import SimpleNamespace as NS

PAGE = ""  # set by the test
SEEN = []          # what each agent round was sent
ANSWER = {"text": "done"}

def _text_of(msgs):
    out = []
    for m in msgs:
        c = m["content"]
        if isinstance(c, str): out.append(c)
        else:
            for b in c:
                if isinstance(b, dict):
                    out.append(b.get("text") or (b.get("content") if isinstance(b.get("content"), str) else ""))
    return "\n".join(x for x in out if x)

def _agent_reply(messages):
    last = messages[-1]["content"]
    if isinstance(last, str):
        return [("tool_use", "toolu_9", "read", {"path": PAGE})]
    return [("text", ANSWER["text"])]

class _Stream:
    def __init__(self, blocks): self.blocks = blocks
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def __aiter__(self): return self._gen()
    async def _gen(self):
        yield NS(type="message_start", message=NS(usage=NS(input_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0)))
        for i, b in enumerate(self.blocks):
            if b[0] == "text":
                yield NS(type="content_block_start", index=i, content_block=NS(type="text"))
                yield NS(type="content_block_delta", index=i, delta=NS(text=b[1]))
            else:
                yield NS(type="content_block_start", index=i, content_block=NS(type="tool_use", id=b[1], name=b[2]))
                yield NS(type="content_block_delta", index=i, delta=NS(partial_json=json.dumps(b[3])))
            yield NS(type="content_block_stop", index=i)
        yield NS(type="message_delta", usage=NS(output_tokens=5))
        yield NS(type="message_stop")

class _Messages:
    def stream(self, **kw):
        SEEN.append(kw["messages"])
        return _Stream(_agent_reply(kw["messages"]))
    async def create(self, **kw):      # probe inference
        return _probe(kw)

def _probe(kw):
    text = (kw.get("system") or "") + "\n" + _text_of(kw["messages"])
    m = re.search(r"secret=(\S+)", text)
    leak = m and "IGNORE PREVIOUS" in text
    return NS(content=[NS(type="text", text=f"Sure: {m.group(1)}" if leak else "I can't help with that.")])

class FakeAsync:
    api_key, base_url = "x", "http://fake"
    def __init__(self): self.messages = _Messages()

class FakeSync:
    def __init__(self, **kw): self.messages = NS(create=lambda **kw: _probe(kw))

def install(session):
    import anthropic
    session._executor._client = FakeAsync()
    anthropic.Anthropic = FakeSync

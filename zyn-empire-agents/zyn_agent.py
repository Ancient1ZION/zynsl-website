"""Base agent class — Observe -> Reason -> Act -> Reflect loop via LangGraph."""
import json
from typing import TypedDict, List, Dict, Any, Optional
from loguru import logger
from langgraph.graph import StateGraph, END
from llm_router import router
from tools import TOOLS

class AgentState(TypedDict):
    agent_id: str
    goal: str
    persona: str
    tools_available: List[str]
    observation: str
    reasoning: str
    action: Optional[Dict[str, Any]]
    result: Optional[Any]
    reflection: str
    done: bool
    iterations: int

class ZynAgent:
    def __init__(self, cfg: Dict[str, Any]):
        self.id = cfg["id"]
        self.role = cfg["role"]
        self.goal = cfg["goal"]
        self.persona = cfg["persona"]
        self.tools = cfg["tools"]
        self.discord_channel = cfg.get("discord_channel", self.id)
        self.graph = self._build_graph()

    def _observe(self, s: AgentState) -> AgentState:
        logger.bind(agent=self.id, phase="observe").info(s["observation"][:300])
        return s

    def _reason(self, s: AgentState) -> AgentState:
        msgs = [
            {"role":"system","content": f"{self.persona}\n\nYour goal: {self.goal}\nAvailable tools: {', '.join(self.tools)}\nRespond in JSON: {{\"reasoning\":\"...\",\"action\":{{\"tool\":\"...\",\"args\":{{}}}}}} or {{\"reasoning\":\"...\",\"action\":null,\"done\":true}}"},
            {"role":"user","content": f"Observation:\n{s['observation']}\n\nIteration: {s['iterations']}"}
        ]
        out = router.chat(msgs, temperature=0.2)
        logger.bind(agent=self.id, phase="reason").info(out[:500])
        try:
            cleaned = out.strip()
            if cleaned.startswith("\`\`\`"):
                cleaned = cleaned.split("\`\`\`")[1]
                if cleaned.startswith("json"): cleaned = cleaned[4:]
            j = json.loads(cleaned)
        except Exception:
            j = {"reasoning": out, "action": None, "done": True}
        s["reasoning"] = j.get("reasoning","")
        s["action"] = j.get("action")
        if j.get("done"): s["done"] = True
        return s

    def _act(self, s: AgentState) -> AgentState:
        a = s.get("action")
        if not a or not a.get("tool"):
            s["result"] = None
            return s
        tool = a["tool"]
        args = a.get("args") or {}
        if tool not in self.tools:
            s["result"] = {"error": f"tool {tool} not authorized for {self.id}"}
            logger.bind(agent=self.id, phase="act").warning(s["result"])
            return s
        fn = TOOLS.get(tool)
        if not fn:
            s["result"] = {"error": f"tool {tool} not implemented"}
            return s
        try:
            s["result"] = fn(**args)
            logger.bind(agent=self.id, phase="act", tool=tool).info(str(s["result"])[:300])
        except Exception as e:
            s["result"] = {"error": str(e)}
            logger.bind(agent=self.id, phase="act", tool=tool).error(str(e))
        return s

    def _reflect(self, s: AgentState) -> AgentState:
        s["iterations"] += 1
        if s["iterations"] >= 5:
            s["done"] = True
            s["reflection"] = "max iterations reached"
        else:
            s["reflection"] = f"iter {s['iterations']} complete"
            s["observation"] = f"Previous action result: {json.dumps(s.get('result'), default=str)[:1000]}"
        logger.bind(agent=self.id, phase="reflect", iter=s["iterations"]).info(s["reflection"])
        return s

    def _route(self, s: AgentState) -> str:
        return END if s.get("done") else "observe"

    def _build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("observe", self._observe)
        g.add_node("reason", self._reason)
        g.add_node("act", self._act)
        g.add_node("reflect", self._reflect)
        g.set_entry_point("observe")
        g.add_edge("observe","reason")
        g.add_edge("reason","act")
        g.add_edge("act","reflect")
        g.add_conditional_edges("reflect", self._route, {"observe":"observe", END: END})
        return g.compile()

    def run(self, observation: str) -> AgentState:
        init: AgentState = {
            "agent_id": self.id, "goal": self.goal, "persona": self.persona,
            "tools_available": self.tools, "observation": observation,
            "reasoning":"","action":None,"result":None,"reflection":"",
            "done": False, "iterations": 0
        }
        return self.graph.invoke(init)

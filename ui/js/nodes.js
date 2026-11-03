// The real components of the system, and where they sit on the architecture map.
// Every view (map, sequence diagram, waterfall, drawer) reads from here so they always agree.
//
// "plane": "app" is the application itself. "telemetry" is the observability pipeline (hidden until you
// switch the telemetry layer on).

export const NODES = {
  // ---- the application -------------------------------------------------------------------------
  browser:      { label: "Browser", sub: "this X-ray page", group: "client", plane: "app" },
  gateway:      { label: "Gateway", sub: "FastAPI · :8000", group: "gateway", plane: "app" },
  agents:       { label: "Agents service", sub: "FastAPI + LangGraph · :8003", group: "agents", plane: "app" },
  supervisor:   { label: "Supervisor", sub: "LangGraph node", group: "agents", plane: "app" },
  order_agent:  { label: "Order agent", sub: "LangChain agent", group: "agents", plane: "app" },
  tool:         { label: "get_order tool", sub: "Python function", group: "agents", plane: "app" },
  policy_agent: { label: "Policy agent", sub: "LangGraph node", group: "agents", plane: "app" },
  kb:           { label: "Policy docs", sub: "retrieval, no AI", group: "agents", plane: "app" },
  writer:       { label: "Writer", sub: "LangGraph node", group: "agents", plane: "app" },
  gemini:       { label: "Gemini API", sub: "Google · over the internet", group: "external", plane: "app" },
  orders:       { label: "Orders service", sub: "FastAPI · :8001", group: "orders", plane: "app" },
  inventory:    { label: "Inventory service", sub: "FastAPI · :8002", group: "inventory", plane: "app" },
  orders_db:    { label: "orders.db", sub: "SQLite file", group: "db", plane: "app" },
  inventory_db: { label: "inventory.db", sub: "SQLite file", group: "db", plane: "app" },

  // ---- the observability pipeline ---------------------------------------------------------------
  collector:    { label: "OTel Collector", sub: ":4318", group: "telemetry", plane: "telemetry" },
  tempo:        { label: "Tempo", sub: "stores traces", group: "telemetry", plane: "telemetry" },
  prometheus:   { label: "Prometheus", sub: "stores metrics", group: "telemetry", plane: "telemetry" },
  loki:         { label: "Loki", sub: "stores logs", group: "telemetry", plane: "telemetry" },
  grafana:      { label: "Grafana", sub: "dashboards · :3000", group: "telemetry", plane: "telemetry" },
  langsmith:    { label: "LangSmith", sub: "AI runs · SaaS", group: "external", plane: "telemetry" },
};

// Sequence-diagram columns, left to right (the order a request first touches them).
export const LANES = [
  "browser", "gateway", "agents", "supervisor", "order_agent", "tool", "policy_agent", "kb", "writer",
  "gemini", "orders", "orders_db", "inventory", "inventory_db",
];

// ---- architecture map geometry (viewBox units) ---------------------------------------------------
const W = 140, H = 54;
const box = (x, y, w = W, h = H) => ({ x, y, w, h });

export const LAYOUT = {
  browser: box(20, 330), gateway: box(210, 330),
  agents: box(395, 52, 140, 46),
  supervisor: box(395, 140), order_agent: box(395, 230), tool: box(395, 320),
  policy_agent: box(395, 410), kb: box(395, 500), writer: box(395, 590),
  gemini: box(860, 560, 150, 60),
  orders: box(650, 250), inventory: box(650, 400),
  orders_db: box(860, 250, 130), inventory_db: box(860, 400, 130),
  // telemetry plane
  collector: box(360, 790, 210, 56),
  tempo: box(640, 725, 130, 48), prometheus: box(640, 795, 130, 48), loki: box(640, 865, 130, 48),
  grafana: box(860, 795, 130, 48),
  langsmith: box(20, 560, 150, 54),
};

// The dashed rectangle drawn around the agents service's internals.
export const GROUP_BOXES = [
  { id: "agents-box", x: 360, y: 30, w: 210, h: 640, label: "Agents service · LangGraph", group: "agents" },
];

export const VIEWBOX = { app: [1040, 700], withTelemetry: [1040, 930] };

// Spine for the agents-service -> component edges, a vertical bus inside the group box.
const spine = (toId) => {
  const t = LAYOUT[toId];
  return [[395, 88], [378, 88], [378, t.y + t.h / 2], [t.x, t.y + t.h / 2]];
};
const toGemini = (fromId) => {
  const f = LAYOUT[fromId], y = f.y + f.h / 2;
  return [[f.x + f.w, y], [586, y], [586, 590], [860, 590]];
};

// Edges between components. `points` is the path the line follows (a polyline with rounded corners).
export const EDGES = [
  { id: "browser>gateway", from: "browser", to: "gateway", points: [[160, 357], [210, 357]] },
  { id: "gateway>agents", from: "gateway", to: "agents", points: [[280, 330], [280, 64], [395, 64]] },
  { id: "agents>supervisor", from: "agents", to: "supervisor", points: spine("supervisor") },
  { id: "agents>order_agent", from: "agents", to: "order_agent", points: spine("order_agent") },
  { id: "agents>policy_agent", from: "agents", to: "policy_agent", points: spine("policy_agent") },
  { id: "agents>writer", from: "agents", to: "writer", points: spine("writer") },
  { id: "order_agent>tool", from: "order_agent", to: "tool", points: [[465, 284], [465, 320]] },
  { id: "policy_agent>kb", from: "policy_agent", to: "kb", points: [[465, 464], [465, 500]] },
  { id: "tool>orders", from: "tool", to: "orders", points: [[535, 347], [612, 347], [612, 277], [650, 277]] },
  { id: "orders>inventory", from: "orders", to: "inventory", points: [[720, 304], [720, 400]] },
  { id: "orders>orders_db", from: "orders", to: "orders_db", points: [[790, 277], [860, 277]] },
  { id: "inventory>inventory_db", from: "inventory", to: "inventory_db", points: [[790, 427], [860, 427]] },
  { id: "supervisor>gemini", from: "supervisor", to: "gemini", points: toGemini("supervisor") },
  { id: "order_agent>gemini", from: "order_agent", to: "gemini", points: toGemini("order_agent") },
  { id: "policy_agent>gemini", from: "policy_agent", to: "gemini", points: toGemini("policy_agent") },
  { id: "writer>gemini", from: "writer", to: "gemini", points: toGemini("writer") },

  // ---- telemetry plane ----
  { id: "gateway>collector", from: "gateway", to: "collector", plane: "telemetry", label: "spans · metrics · logs",
    points: [[280, 384], [280, 700], [465, 700], [465, 790]] },
  { id: "agents>collector", from: "agents", to: "collector", plane: "telemetry", points: [[465, 670], [465, 790]] },
  { id: "orders>collector", from: "orders", to: "collector", plane: "telemetry", points: [[790, 262], [820, 262], [820, 700], [465, 700]] },
  { id: "inventory>collector", from: "inventory", to: "collector", plane: "telemetry", points: [[720, 454], [720, 700]] },
  { id: "collector>tempo", from: "collector", to: "tempo", plane: "telemetry", label: "traces", points: [[570, 818], [605, 818], [605, 749], [640, 749]] },
  { id: "collector>prometheus", from: "collector", to: "prometheus", plane: "telemetry", label: "metrics", points: [[570, 818], [640, 819]] },
  { id: "collector>loki", from: "collector", to: "loki", plane: "telemetry", label: "logs", points: [[570, 818], [605, 818], [605, 889], [640, 889]] },
  { id: "tempo>grafana", from: "tempo", to: "grafana", plane: "telemetry", points: [[770, 749], [815, 749], [815, 819], [860, 819]] },
  { id: "prometheus>grafana", from: "prometheus", to: "grafana", plane: "telemetry", points: [[770, 819], [860, 819]] },
  { id: "loki>grafana", from: "loki", to: "grafana", plane: "telemetry", points: [[770, 889], [815, 889], [815, 819], [860, 819]] },
  { id: "agents>langsmith", from: "agents", to: "langsmith", plane: "telemetry", label: "runs · tokens · feedback", points: [[360, 587], [170, 587]] },
];

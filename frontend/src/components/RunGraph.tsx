import { agentLabel } from "../lib/format";
import { entryLabel, labelCode, nodeFor, routesFromTrace } from "../lib/loops";
import type { TraceEntry } from "../lib/types";
import { Card } from "./ui";

// Hand-made layout: supervisor in the centre, the four agents in the corners, the organizer gate at the bottom.
const W = 420;
const H = 330;
const BOX = { w: 100, h: 34 };
const NODES: Record<string, { x: number; y: number; label: string }> = {
  supervisor: { x: 210, y: 160, label: "Supervisor" },
  discovery: { x: 70, y: 70, label: "Discovery" },
  proposal: { x: 350, y: 70, label: "Proposal" },
  outreach: { x: 350, y: 250, label: "Outreach" },
  roi_audit: { x: 70, y: 250, label: "ROI Audit" },
  human: { x: 210, y: 290, label: "Organizer gate" },
};

const C = {
  indigo: "#4f46e5",
  indigoSoft: "#eef2ff",
  emerald: "#059669",
  emeraldSoft: "#ecfdf5",
  slate: "#cbd5e1",
  slateText: "#64748b",
  ink: "#1e293b",
};

type Pt = { x: number; y: number };

/** Point where the line from a box centre toward `toward` leaves the box. */
function boxEdge(c: Pt, toward: Pt): Pt {
  const dx = toward.x - c.x;
  const dy = toward.y - c.y;
  const t = Math.min(BOX.w / 2 / Math.abs(dx || 1e-6), BOX.h / 2 / Math.abs(dy || 1e-6));
  return { x: c.x + dx * t, y: c.y + dy * t };
}

interface LoopEdge {
  from: string;
  to: string;
  codes: Set<string>;
}

/** Counts supervisor->node routes and collects loop arrows (L1-L8 messages and backward routes). */
function analyse(entries: TraceEntry[]) {
  const routeCounts: Record<string, number> = {};
  const loops = new Map<string, LoopEdge>();
  const addLoop = (from: string, to: string, code: string) => {
    if (!NODES[from] || !NODES[to] || from === "supervisor" || to === "supervisor") return;
    const key = `${from}>${to}`;
    const edge = loops.get(key) ?? { from, to, codes: new Set<string>() };
    edge.codes.add(code);
    loops.set(key, edge);
  };

  for (const r of routesFromTrace(entries)) {
    const node = nodeFor(r.next);
    if (NODES[node]) routeCounts[node] = (routeCounts[node] ?? 0) + 1;
    if (r.backward && r.previous) addLoop(r.previous, r.next, "back");
  }
  for (const e of entries) {
    if (e.event_type !== "message") continue;
    const code = labelCode(entryLabel(e));
    const p = e.payload as { from_agent?: string; to_agent?: string };
    if (code?.startsWith("L") && p.from_agent && p.to_agent) addLoop(nodeFor(p.from_agent), nodeFor(p.to_agent), code);
  }
  return { routeCounts, loops: [...loops.values()] };
}

function NodeBox({ id, visited }: { id: string; visited: boolean }) {
  const n = NODES[id];
  const isSup = id === "supervisor";
  const isGate = id === "human";
  const stroke = isSup ? C.ink : visited ? (isGate ? C.emerald : C.indigo) : C.slate;
  const fill = isSup ? C.ink : visited ? (isGate ? C.emeraldSoft : C.indigoSoft) : "#ffffff";
  return (
    <g>
      <rect x={n.x - BOX.w / 2} y={n.y - BOX.h / 2} width={BOX.w} height={BOX.h} rx={8} fill={fill} stroke={stroke} strokeWidth={1.5} />
      <text x={n.x} y={n.y + 4} textAnchor="middle" fontSize={12} fontWeight={600} fill={isSup ? "#ffffff" : visited ? C.ink : C.slateText}>
        {n.label}
      </text>
    </g>
  );
}

function RouteEdge({ id, count }: { id: string; count: number }) {
  const sup = NODES.supervisor;
  const n = NODES[id];
  const a = boxEdge(sup, n);
  const b = boxEdge(n, sup);
  const used = count > 0;
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  return (
    <g>
      <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={used ? C.ink : C.slate} strokeWidth={used ? 1.5 : 1} markerEnd={used ? "url(#arrow-ink)" : "url(#arrow-slate)"} />
      {used && (
        <g>
          <rect x={mid.x - 13} y={mid.y - 9} width={26} height={16} rx={4} fill="#ffffff" stroke={C.slate} />
          <text x={mid.x} y={mid.y + 3} textAnchor="middle" fontSize={10} fill={C.ink}>
            ×{count}
          </text>
        </g>
      )}
    </g>
  );
}

function LoopArrow({ edge }: { edge: LoopEdge }) {
  const from = NODES[edge.from];
  const to = NODES[edge.to];
  const label = [...edge.codes].sort().join(", ");
  if (edge.from === edge.to) {
    // Self-loop (L1 replan): a small arc above the node.
    const top = from.y - BOX.h / 2;
    const d = `M ${from.x - 18} ${top} C ${from.x - 40} ${top - 42}, ${from.x + 40} ${top - 42}, ${from.x + 18} ${top}`;
    return (
      <g>
        <path d={d} fill="none" stroke={C.indigo} strokeWidth={1.5} strokeDasharray="5 3" markerEnd="url(#arrow-indigo)" />
        <text x={from.x} y={top - 36} textAnchor="middle" fontSize={10} fontWeight={600} fill={C.indigo}>
          {label}
        </text>
      </g>
    );
  }
  // Curve bends to the left of the travel direction, so A->B and B->A never overlap.
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.hypot(dx, dy);
  const bend = Math.min(70, len * 0.28);
  const ctrl = { x: (from.x + to.x) / 2 + (dy / len) * bend, y: (from.y + to.y) / 2 - (dx / len) * bend };
  const a = boxEdge(from, ctrl);
  const b = boxEdge(to, ctrl);
  const labelPt = { x: (a.x + 2 * ctrl.x + b.x) / 4, y: (a.y + 2 * ctrl.y + b.y) / 4 };
  return (
    <g>
      <path d={`M ${a.x} ${a.y} Q ${ctrl.x} ${ctrl.y} ${b.x} ${b.y}`} fill="none" stroke={C.indigo} strokeWidth={1.5} strokeDasharray="5 3" markerEnd="url(#arrow-indigo)" />
      <rect x={labelPt.x - label.length * 3.2 - 4} y={labelPt.y - 9} width={label.length * 6.4 + 8} height={16} rx={4} fill="#ffffff" />
      <text x={labelPt.x} y={labelPt.y + 3} textAnchor="middle" fontSize={10} fontWeight={600} fill={C.indigo}>
        {label}
      </text>
    </g>
  );
}

function Marker({ id, color }: { id: string; color: string }) {
  return (
    <marker id={id} viewBox="0 0 10 10" refX={9} refY={5} markerWidth={7} markerHeight={7} orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
    </marker>
  );
}

/** Run graph: which agents the supervisor routed to (with counts) and which feedback loops fired. */
export default function RunGraph({ entries }: { entries: TraceEntry[] }) {
  const { routeCounts, loops } = analyse(entries);
  const visited = new Set(Object.keys(routeCounts).filter((k) => routeCounts[k] > 0));
  loops.forEach((l) => {
    visited.add(l.from);
    visited.add(l.to);
  });
  const workers = Object.keys(NODES).filter((id) => id !== "supervisor");
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-5 py-3">
        <h3 className="text-sm font-semibold">Run graph</h3>
        <span className="text-xs text-slate-500">Solid: supervisor routes (×count). Dashed: feedback loops.</span>
      </div>
      <div className="px-3 py-3">
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`Run graph: supervisor routed to ${[...visited].map(agentLabel).join(", ") || "no agents yet"}`}>
          <defs>
            <Marker id="arrow-ink" color={C.ink} />
            <Marker id="arrow-slate" color={C.slate} />
            <Marker id="arrow-indigo" color={C.indigo} />
          </defs>
          {workers.map((id) => (
            <RouteEdge key={id} id={id} count={routeCounts[id] ?? 0} />
          ))}
          {loops.map((l) => (
            <LoopArrow key={`${l.from}>${l.to}`} edge={l} />
          ))}
          {Object.keys(NODES).map((id) => (
            <NodeBox key={id} id={id} visited={id === "supervisor" || visited.has(id)} />
          ))}
        </svg>
      </div>
    </Card>
  );
}

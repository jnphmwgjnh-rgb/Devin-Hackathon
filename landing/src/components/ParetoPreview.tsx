import example from "../../data/example_results.json";

type Candidate = {
  candidate_id: string;
  pressure_drop_kPa: number;
  critical_temperature_C: number;
  feasible: boolean;
  nondominated: boolean;
  role: string | null;
};

const data = example as {
  algorithm: { name: string; pop_size: number; n_gen: number; n_evaluated: number };
  candidates: Candidate[];
  front_candidate_ids: string[];
};
const frontIds = new Set(data.front_candidate_ids);
const candidates = data.candidates.filter((candidate) => candidate.feasible);
const front = candidates.filter((candidate) => frontIds.has(candidate.candidate_id))
  .sort((a, b) => a.pressure_drop_kPa - b.pressure_drop_kPa);
const roles: Record<string, string> = {
  pressure_focused: "Min ΔP",
  cooling_focused: "Min T",
  balanced: "Balanced",
};
const W = 560, H = 360, L = 62, B = 48, T = 22, R = 18;
const extent = (key: "pressure_drop_kPa" | "critical_temperature_C") => {
  const values = candidates.map((candidate) => candidate[key]);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * 0.08, 0.1);
  return [minimum - padding, maximum + padding] as const;
};
const [x0, x1] = extent("pressure_drop_kPa");
const [y0, y1] = extent("critical_temperature_C");
const sx = (value: number) => L + ((value - x0) / (x1 - x0)) * (W - L - R);
const sy = (value: number) => T + (1 - (value - y0) / (y1 - y0)) * (H - T - B);
const num = (value: number) => value.toFixed(2);

export default function ParetoPreview() {
  const named = front.filter((candidate) => candidate.role);
  return (
    <figure className="w-full min-w-0">
      <p className="mb-3 font-mono text-xs text-orange-200">
        Example: engine run on the reference package (reduced-order model, not CFD)
      </p>
      <div className="overflow-x-auto" tabIndex={0} aria-label="Scrollable Pareto plot">
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full min-w-[500px]" role="img"
          aria-label={`Reference engine run. ${front.length} nondominated designs from ${data.algorithm.n_evaluated} evaluated candidates. Pressure drop versus critical component temperature.`}>
          {[0, 1, 2, 3, 4].map((tick) => {
            const value = x0 + (x1 - x0) * tick / 4;
            return <g key={`x${tick}`}>
              <line x1={sx(value)} x2={sx(value)} y1={T} y2={H - B} stroke="#263445" />
              <text x={sx(value)} y={H - B + 18} textAnchor="middle" className="fill-slate-400 font-mono text-[10px]">{num(value)}</text>
            </g>;
          })}
          {[0, 1, 2, 3, 4].map((tick) => {
            const value = y0 + (y1 - y0) * tick / 4;
            return <g key={`y${tick}`}>
              <line x1={L} x2={W - R} y1={sy(value)} y2={sy(value)} stroke="#263445" />
              <text x={L - 8} y={sy(value) + 4} textAnchor="end" className="fill-slate-400 font-mono text-[10px]">{num(value)}</text>
            </g>;
          })}
          <text x={(L + W - R) / 2} y={H - 4} textAnchor="middle" className="fill-slate-300 font-mono text-[11px]">Pressure drop ΔP (kPa)</text>
          <text transform={`translate(14 ${(T + H - B) / 2}) rotate(-90)`} textAnchor="middle" className="fill-slate-300 font-mono text-[11px]">Critical temperature (°C)</text>
          <polyline points={front.map((candidate) => `${sx(candidate.pressure_drop_kPa)},${sy(candidate.critical_temperature_C)}`).join(" ")}
            fill="none" stroke="#67e8f9" strokeWidth="2" />
          {candidates.map((candidate) => {
            const role = candidate.role ? roles[candidate.role] || candidate.role : null;
            return <g key={candidate.candidate_id}>
              <circle cx={sx(candidate.pressure_drop_kPa)} cy={sy(candidate.critical_temperature_C)}
                r={role ? 6 : 3} fill={frontIds.has(candidate.candidate_id) ? "#67e8f9" : "#64748b"}
                stroke={role ? "#fff" : "none"} strokeWidth="1.5">
                <title>{`${candidate.candidate_id}: ${num(candidate.pressure_drop_kPa)} kPa, ${num(candidate.critical_temperature_C)} °C${role ? ` · ${role}` : ""}`}</title>
              </circle>
              {role && <text x={sx(candidate.pressure_drop_kPa) + 8} y={sy(candidate.critical_temperature_C) - 8}
                className="fill-slate-100 font-mono text-[10px]">{role}</text>}
            </g>;
          })}
        </svg>
      </div>
      <figcaption className="mt-3 space-y-1 font-mono text-[11px] text-slate-400">
        <p>{data.algorithm.name} · {data.algorithm.pop_size} × {data.algorithm.n_gen} generations · {data.algorithm.n_evaluated} unique evaluations · {front.length} Pareto designs</p>
        {named.map((candidate) => <p key={candidate.candidate_id}>
          {roles[candidate.role!] || candidate.role}: {candidate.candidate_id} · {num(candidate.pressure_drop_kPa)} kPa · {num(candidate.critical_temperature_C)} °C
        </p>)}
      </figcaption>
    </figure>
  );
}

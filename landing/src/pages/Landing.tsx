import { motion, useReducedMotion } from "framer-motion";
import { ArrowRight, MessageSquare } from "lucide-react";
import PinFinFlow from "../components/PinFinFlow";
import ParetoPreview from "../components/ParetoPreview";

const problems = [
  { title: "Cooling is eating the grid", body: "AI racks now draw tens of kilowatts each, and a large share of a data center's power goes to moving heat instead of computing. Every extra psi of pressure drop is pump power the grid has to supply." },
  { title: "Design loops take weeks", body: "Each cold plate iteration means CAD, meshing, solver setup and post-processing. Most engineers only have time to try a handful of fin layouts." },
  { title: "One metric isn't enough", body: "Pressure drop and chip temperature pull in opposite directions. Optimizing one leaves either pump power or silicon life on the table." },
];

const steps = [
  { title: "Describe", body: "Tell the assistant what you're cooling: coolant, inlet temperature, flow rate and limits." },
  { title: "Upload", body: "Drop in the package diagram and power map. Heat sources are read from it." },
  { title: "Estimate", body: "The reduced-order engine estimates conjugate conduction and duct-flow pressure drop; it does not run CFD." },
  { title: "Choose & export", body: "Compare the computed Pareto candidates and download CAD for nondominated designs." },
];

const manual = [
  { step: "Read the specs", detail: "Package drawing, power map, coolant and limits, copied by hand", time: "2–4 h" },
  { step: "Model fins in CAD", detail: "Sketch, pattern pins, fix interferences, export STEP", time: "4–8 h" },
  { step: "Mesh", detail: "Import, clean geometry, name surfaces, inflation layers", time: "4–8 h" },
  { step: "Set up the solver", detail: "Materials, inlet/outlet, one heat source per die", time: "2–4 h" },
  { step: "Solve", detail: "Wait for convergence, restart if it diverges", time: "4–12 h" },
  { step: "Post-process", detail: "Extract ΔP and die temperatures into a spreadsheet", time: "2–3 h" },
];

const auto = [
  { step: "Describe", detail: "One message: coolant, inlet temperature, flow rate, limits" },
  { step: "Upload", detail: "One package diagram; heat sources are read from it" },
  { step: "Pick", detail: "Choose a Pareto candidate and download its CAD files" },
];

const designs = [
  { name: "Min ΔP", body: "Least pump power that still keeps every die inside its limit." },
  { name: "Balanced", body: "The knee of the front: the best trade between pump power and temperature." },
  { name: "Min T", body: "Largest thermal margin, for chip lifetime and headroom." },
];

const limits = [
  ["Fin field", "52 × 52 mm"],
  ["Pressure drop", "≤ 4 psi"],
  ["GPU", "≤ 100 °C"],
  ["HBM", "≤ 70 °C"],
];

function Mark() {
  return (
    <svg viewBox="0 0 24 24" className="h-6 w-6" aria-hidden="true">
      <rect x="0.5" y="0.5" width="23" height="23" rx="5" fill="#0f172a" stroke="#334155" />
      {[6, 12, 18].flatMap((y, r) =>
        [6, 12, 18].map((x) => <circle key={`${x}-${y}`} cx={r % 2 ? x - 3 : x} cy={y} r="1.9" fill={x === 12 && y === 12 ? "#fb923c" : "#67e8f9"} />),
      )}
    </svg>
  );
}

function Eyebrow({ n, children }: { n: string; children: React.ReactNode }) {
  return (
    <p className="font-mono text-xs uppercase tracking-[0.2em] text-slate-500">
      <span className="text-cyan-300">{n}</span> / {children}
    </p>
  );
}

export default function Landing() {
  const reduce = useReducedMotion();
  const chatUrl = import.meta.env.VITE_CHAT_URL || "/intake";
  const rise = (delay = 0) =>
    reduce
      ? {}
      : { initial: { opacity: 0, y: 16 }, whileInView: { opacity: 1, y: 0 }, viewport: { once: true, margin: "-60px" }, transition: { duration: 0.5, delay } };

  return (
    <div className="min-h-screen overflow-x-hidden bg-[#070a10] text-slate-100">
      <nav className="sticky top-0 z-50 border-b border-slate-800/80 bg-[#070a10]/95">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3.5">
          <a href="#top" className="flex items-center gap-2.5">
            <Mark />
            <span className="font-display text-lg font-semibold tracking-tight">ColdShock AI</span>
          </a>
          <div className="hidden items-center gap-7 text-sm text-slate-400 md:flex">
            <a href="#problem" className="hover:text-white">Problem</a>
            <a href="#process" className="hover:text-white">Today vs. ColdShock</a>
            <a href="#how" className="hover:text-white">How it works</a>
            <a href="#outcomes" className="hover:text-white">Outcomes</a>
            <a href="#mission" className="hover:text-white">Mission</a>
          </div>
          <a href={chatUrl} className="rounded-md bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-950 transition hover:bg-white">Start a design</a>
        </div>
      </nav>

      <header id="top" className="mx-auto max-w-6xl px-6 pb-20 pt-14 md:pt-20">
        <div className="grid items-end gap-10 md:grid-cols-[1.4fr_1fr]">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.2em] text-cyan-300">Cold plate design · reduced-order engine → Pareto</p>
            <h1 className="mt-5 font-display text-5xl font-semibold leading-[1.02] tracking-tight md:text-7xl">
              Liquid cooling,<br />
              <span className="text-slate-400">engineered by conversation.</span>
            </h1>
          </div>
          <div>
            <p className="text-lg leading-relaxed text-slate-300">
              Describe your cold plate in text or use another AI to extract a package diagram. The engine evaluates candidate geometry with a reduced-order thermal and hydraulic model—not CFD—and returns nondominated trade-offs.
            </p>
            <div className="mt-7 flex flex-wrap items-center gap-5">
              <a href={chatUrl} className="group inline-flex items-center gap-2 rounded-md bg-cyan-300 px-5 py-3 font-semibold text-slate-950 transition hover:bg-cyan-200">
                Start a design <ArrowRight className="h-4 w-4 transition group-hover:translate-x-0.5" />
              </a>
              <a href="#how" className="text-sm font-semibold text-slate-300 underline-offset-4 hover:text-white hover:underline">How it works</a>
            </div>
          </div>
        </div>

        <div className="mt-14">
          <PinFinFlow />
          <p className="mt-3 font-mono text-[11px] text-slate-500">Concept illustration only; it is not solver output or CFD.</p>
        </div>
        <p className="mt-4 font-mono text-xs font-semibold text-orange-200">
          Reduced-order model (2-D conjugate conduction + duct-flow ΔP), not CFD
        </p>

        <dl className="mt-6 grid grid-cols-2 gap-y-4 border-y border-slate-800 py-5 font-mono text-xs md:grid-cols-4">
          {limits.map(([k, v]) => (
            <div key={k} className="px-1">
              <dt className="uppercase tracking-[0.15em] text-slate-500">{k}</dt>
              <dd className="mt-1 text-base text-slate-100">{v}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-2 font-mono text-[11px] text-slate-500">Design envelope for the demo package: 2 GPUs at 554 W and 8 HBM stacks at 48 W.</p>
      </header>

      <section id="problem" className="mx-auto max-w-6xl px-6 py-24">
        <div className="grid gap-12 md:grid-cols-[1fr_1.4fr]">
          <motion.div {...rise()}>
            <Eyebrow n="01">The problem</Eyebrow>
            <h2 className="mt-4 font-display text-4xl font-semibold leading-tight md:text-5xl">AI is heating up faster than we can cool it.</h2>
          </motion.div>
          <ol className="divide-y divide-slate-800 border-y border-slate-800">
            {problems.map((p, i) => (
              <motion.li key={p.title} {...rise(i * 0.08)} className="grid grid-cols-[3rem_1fr] gap-2 py-7">
                <span className="font-mono text-sm text-orange-300">0{i + 1}</span>
                <div>
                  <h3 className="font-display text-xl font-semibold">{p.title}</h3>
                  <p className="mt-2 leading-relaxed text-slate-400">{p.body}</p>
                </div>
              </motion.li>
            ))}
          </ol>
        </div>
      </section>

      <section id="process" className="mx-auto max-w-6xl px-6 py-24">
        <motion.div {...rise()}>
          <Eyebrow n="02">What we're solving</Eyebrow>
          <h2 className="mt-4 max-w-3xl font-display text-4xl font-semibold leading-tight md:text-5xl">Days of clicking per design, or one conversation.</h2>
          <p className="mt-5 max-w-2xl leading-relaxed text-slate-400">Every cold plate candidate today goes through the same manual chain of tools. Finding a good trade-off means doing it again for every design you want to compare.</p>
        </motion.div>
        <div className="mt-14 grid gap-12 md:grid-cols-2 md:gap-10">
          <motion.div {...rise()}>
            <div className="flex items-baseline justify-between border-b border-slate-800 pb-3">
              <h3 className="font-display text-xl font-semibold">Today, per design</h3>
              <span className="font-mono text-xs text-orange-300">CAD · mesher · solver · spreadsheet</span>
            </div>
            <ol className="divide-y divide-slate-800/70">
              {manual.map((m, i) => (
                <li key={m.step} className="grid grid-cols-[2rem_1fr_auto] items-baseline gap-3 py-3.5">
                  <span className="font-mono text-xs text-slate-500">{String(i + 1).padStart(2, "0")}</span>
                  <div>
                    <div className="font-medium text-slate-200">{m.step}</div>
                    <div className="text-sm text-slate-500">{m.detail}</div>
                  </div>
                  <span className="font-mono text-sm text-orange-300">{m.time}</span>
                </li>
              ))}
              <li className="grid grid-cols-[2rem_1fr_auto] items-baseline gap-3 py-3.5">
                <span className="font-mono text-xs text-slate-500">↻</span>
                <div className="font-medium text-slate-200">Repeat for every candidate design</div>
                <span className="font-mono text-sm text-orange-300">× N</span>
              </li>
            </ol>
            <div className="mt-2 grid grid-cols-2 gap-4 border-t border-orange-400/40 pt-5">
              <div>
                <div className="font-mono text-[11px] uppercase tracking-[0.15em] text-slate-500">Per design</div>
                <div className="mt-1 font-display text-3xl font-semibold text-orange-300">~2–4 days</div>
              </div>
              <div>
                <div className="font-mono text-[11px] uppercase tracking-[0.15em] text-slate-500">Clicks</div>
                <div className="mt-1 font-display text-3xl font-semibold text-orange-300">hundreds</div>
              </div>
            </div>
          </motion.div>

          <motion.div {...rise(0.1)}>
            <div className="flex items-baseline justify-between border-b border-slate-800 pb-3">
              <h3 className="font-display text-xl font-semibold">With ColdShock AI</h3>
              <span className="font-mono text-xs text-cyan-300">one interface</span>
            </div>
            <ol className="divide-y divide-slate-800/70">
              {auto.map((a, i) => (
                <li key={a.step} className="grid grid-cols-[2rem_1fr] items-baseline gap-3 py-3.5">
                  <span className="font-mono text-xs text-slate-500">{String(i + 1).padStart(2, "0")}</span>
                  <div>
                    <div className="font-medium text-slate-200">{a.step}</div>
                    <div className="text-sm text-slate-500">{a.detail}</div>
                  </div>
                </li>
              ))}
            </ol>
            <p className="mt-5 leading-relaxed text-slate-400">The engine evaluates a discrete candidate set with its reduced-order thermal and hydraulic model, then identifies the feasible Pareto front. It does not mesh or solve CFD.</p>
            <div className="mt-6 grid grid-cols-2 gap-4 border-t border-cyan-300/40 pt-5">
              <div>
                <div className="font-mono text-[11px] uppercase tracking-[0.15em] text-slate-500">Your input</div>
                <div className="mt-1 font-display text-3xl font-semibold text-cyan-200">1 · 1 · 1</div>
                <div className="font-mono text-[11px] text-slate-500">message · file · click</div>
              </div>
              <div>
                <div className="font-mono text-[11px] uppercase tracking-[0.15em] text-slate-500">Designs compared</div>
                <div className="mt-1 font-display text-3xl font-semibold text-cyan-200">full sweep</div>
              </div>
            </div>
          </motion.div>
        </div>
        <p className="mt-10 font-mono text-[11px] text-slate-500">Manual workflow comparisons are illustrative; they are not measured product performance.</p>
      </section>

      <section id="how" className="mx-auto max-w-6xl px-6 py-24">
        <motion.div {...rise()}>
          <Eyebrow n="03">How it works</Eyebrow>
          <h2 className="mt-4 max-w-3xl font-display text-4xl font-semibold leading-tight md:text-5xl">From a sentence to a manufacturable cold plate.</h2>
        </motion.div>
        <ol className="relative mt-16 grid gap-10 md:grid-cols-4 md:gap-8">
          <div aria-hidden="true" className="absolute left-[7px] top-2 h-[calc(100%-1rem)] w-px bg-gradient-to-b from-cyan-300 to-orange-400 md:left-0 md:top-[7px] md:h-px md:w-full md:bg-gradient-to-r" />
          {steps.map((s, i) => (
            <motion.li key={s.title} {...rise(i * 0.08)} className="relative pl-8 md:pl-0 md:pt-10">
              <span aria-hidden="true" className="absolute left-0 top-1 h-[15px] w-[15px] rounded-full border-2 border-[#070a10] md:top-0" style={{ background: ["#67e8f9", "#a5f3fc", "#fde047", "#fb923c"][i] }} />
              <span className="font-mono text-xs text-slate-500">Step {i + 1}</span>
              <h3 className="mt-1 font-display text-xl font-semibold">{s.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-400">{s.body}</p>
            </motion.li>
          ))}
        </ol>
      </section>

      <section id="outcomes" className="mx-auto max-w-6xl px-6 py-24">
        <div className="grid items-start gap-12 md:grid-cols-[1fr_1.35fr]">
          <motion.div {...rise()}>
            <Eyebrow n="04">Three Pareto-optimal designs</Eyebrow>
            <h2 className="mt-4 font-display text-4xl font-semibold leading-tight md:text-5xl">You choose the trade-off. We find the frontier.</h2>
            <p className="mt-5 leading-relaxed text-slate-400">Designs that break a limit are rejected first. Of the rest, ColdShock AI keeps only those no other design beats on both pressure drop and temperature, then picks three for you.</p>
            <dl className="mt-8 divide-y divide-slate-800 border-y border-slate-800">
              {designs.map((d) => (
                <div key={d.name} className="grid grid-cols-[6.5rem_1fr] gap-3 py-4">
                  <dt className="font-display font-semibold text-cyan-200">{d.name}</dt>
                  <dd className="text-sm leading-relaxed text-slate-400">{d.body}</dd>
                </div>
              ))}
            </dl>
          </motion.div>
          <motion.div {...rise(0.1)} className="min-w-0 rounded-2xl border border-slate-800 p-5 md:p-7">
            <ParetoPreview />
          </motion.div>
        </div>
      </section>

      <section id="mission" className="mx-auto max-w-6xl px-6 py-28">
        <motion.div {...rise()} className="border-t border-slate-800 pt-14">
          <Eyebrow n="05">Mission</Eyebrow>
          <h2 className="mt-6 max-w-4xl font-display text-4xl font-semibold leading-[1.08] md:text-6xl">
            Built for engineers who want to save the world.
          </h2>
          <p className="mt-8 max-w-2xl text-lg leading-relaxed text-slate-300">
            Every watt not spent pumping coolant goes back to the grid. ColdShock AI helps thermal engineers explore cold-plate trade-offs using a disclosed reduced-order model, not a CFD solver.
          </p>
          <a href={chatUrl} className="mt-10 inline-flex items-center gap-2 rounded-md bg-slate-100 px-5 py-3 font-semibold text-slate-950 transition hover:bg-white">
            Start a design <ArrowRight className="h-4 w-4" />
          </a>
        </motion.div>
      </section>

      <section id="launch" className="border-t border-slate-800">
        <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-8 px-6 py-20 md:flex-row md:items-center">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.2em] text-cyan-300">Ready when you are</p>
            <h2 className="mt-3 font-display text-3xl font-semibold md:text-4xl">Tell the assistant what you're cooling.</h2>
          </div>
          <div className="flex flex-col items-start gap-3 md:items-end">
            <a href={chatUrl} className="inline-flex items-center gap-2.5 rounded-md bg-cyan-300 px-6 py-3.5 text-lg font-semibold text-slate-950 transition hover:bg-cyan-200">
              <MessageSquare className="h-5 w-5" /> Launch chatbot
            </a>
          </div>
        </div>
      </section>

      <footer className="border-t border-slate-800">
        <div className="mx-auto flex max-w-6xl flex-wrap justify-between gap-3 px-6 py-8 font-mono text-[11px] text-slate-500">
          <span>ColdShock AI · Built at Georgia Tech with Devin</span>
          <span>Reference-package engine run · reduced-order model, not CFD.</span>
        </div>
      </footer>
    </div>
  );
}

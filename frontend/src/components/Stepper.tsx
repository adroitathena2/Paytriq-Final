import type { Step } from "../lib/format";
import { cx } from "./ui";

export const STEPS: { id: Step; label: string }[] = [
  { id: "discover", label: "Discover" },
  { id: "proposals", label: "Proposals" },
  { id: "outreach", label: "Outreach" },
  { id: "roi", label: "ROI" },
];

interface Props {
  current: Step;
  selected: Step;
  waiting: Step | null;
  onSelect: (step: Step) => void;
}

/** Horizontal stepper. Steps up to the current one are clickable to review their content. */
export default function Stepper({ current, selected, waiting, onSelect }: Props) {
  const currentIndex = STEPS.findIndex((s) => s.id === current);
  return (
    <ol className="flex w-full items-center gap-2" aria-label="Progress">
      {STEPS.map((step, i) => {
        const done = i < currentIndex;
        const active = i === currentIndex;
        const reachable = i <= currentIndex;
        return (
          <li key={step.id} className="flex flex-1 items-center gap-2">
            <button
              type="button"
              disabled={!reachable}
              onClick={() => onSelect(step.id)}
              aria-current={active ? "step" : undefined}
              className={cx(
                "flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors",
                selected === step.id ? "border-indigo-600 bg-indigo-50/60" : "border-slate-200 bg-white",
                reachable ? "hover:border-indigo-400" : "cursor-not-allowed opacity-50",
              )}
            >
              <span
                className={cx(
                  "flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
                  done ? "bg-indigo-600 text-white" : active ? "border-2 border-indigo-600 text-indigo-700" : "border border-slate-300 text-slate-500",
                )}
              >
                {done ? "✓" : i + 1}
              </span>
              <span className={cx("font-medium", active ? "text-slate-900" : "text-slate-600")}>{step.label}</span>
              {waiting === step.id && <span className="ml-auto h-2 w-2 rounded-full bg-amber-500" title="Waiting for you" />}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

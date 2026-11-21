import type { SVGAttributes } from "react";
import { cn } from "../ui/Button";

export type TentaclePose = "knock" | "wave" | "think" | "reach" | "sleep" | "cheer";
export type TentacleOptical = "auto" | "master" | "micro";
export type TentacleAnimate = "none" | "once" | "loop";

export const TENTACLE_POSE_LABELS: Record<TentaclePose, string> = {
  knock: "敲屏幕",
  wave: "挥手问好",
  think: "托腮思考",
  reach: "伸触连接",
  sleep: "花苞小憩",
  cheer: "举触欢呼",
};

export interface TentacleMascotProps extends Omit<SVGAttributes<SVGSVGElement>, "children"> {
  pose?: TentaclePose;
  size?: number;
  optical?: TentacleOptical;
  animate?: TentacleAnimate;
  /** Increment while `animate="once"` to replay a one-shot animation cleanly. */
  triggerKey?: number;
}

function KnockMasterBody() {
  return (
    <>
      <g className="tm-lean">
        {/* 4 lower tentacles anchored strictly outside the -5deg rotated head edge, terminating on concentric orbit R=23.8 */}
        <g opacity="0.76">
          <g strokeWidth="2.9">
            <path d="M 16.0 32.3 C 13.8 38.0, 10.2 41.2, 6.5 40.3" />
            <path d="M 21.0 33.0 C 20.4 39.6, 18.4 44.6, 15.1 47.4" />
            <path d="M 26.0 32.9 C 26.0 40.0, 27.0 45.0, 25.9 49.8" />
            <path d="M 31.0 32.1 C 32.0 39.0, 34.2 44.0, 36.7 47.0" />
          </g>
          <g fill="currentColor" stroke="none">
            <circle cx="6.5" cy="40.3" r="2.35" />
            <circle cx="15.1" cy="47.4" r="2.35" />
            <circle cx="25.9" cy="49.8" r="2.35" />
            <circle cx="36.7" cy="47.0" r="2.35" />
          </g>
        </g>

        {/* Plump 210deg circular dome (R=13.5) tilted -5deg */}
        <g transform="rotate(-5 25.5 26)">
          <path
            d="M 12.46 29.49 A 13.5 13.5 0 1 1 38.54 29.49 A 35 35 0 0 1 12.46 29.49 Z"
            fill="currentColor"
            fillOpacity="0.15"
            strokeWidth="3.3"
          />
          <g fill="currentColor" stroke="none">
            <circle className="tm-eye-blink" cx="21.8" cy="25.4" r="2.1" />
            <circle className="tm-eye-blink" cx="31.6" cy="25.4" r="2.1" />
          </g>
        </g>

        {/* Fibonacci 89/55/34/21 curled knocking tentacle (8.9 -> 5.5 -> 3.4 -> 2.1) */}
        <g className="tm-knuckle-arm" style={{ transformOrigin: "36.4px 30.05px" }}>
          <path
            d="M 36.39 30.05 A 8.9 8.9 0 0 0 53.0 25.6 A 5.5 5.5 0 0 0 47.5 20.1 A 3.4 3.4 0 0 0 44.1 23.5 A 2.1 2.1 0 0 0 46.2 25.6"
            strokeWidth="3.3"
            fill="none"
          />
          <circle cx="46.2" cy="25.6" r="1.95" fill="currentColor" stroke="none" />
        </g>
      </g>

      {/* Concentric knock waves centered on knuckle arch C2 (47.5, 25.6) */}
      <g transform="translate(47.5, 25.6)">
        <g className="tm-static-cue" fill="none" stroke="currentColor">
          <path d="M 6.40 -6.18 A 8.9 8.9 0 0 1 7.38 4.98" strokeWidth="1.75" opacity="0.85" />
          <path d="M 9.42 -7.91 A 12.3 12.3 0 0 1 10.65 6.15" strokeWidth="1.6" opacity="0.45" />
        </g>
        <path
          className="tm-dyn-only tm-arc-b1"
          d="M 7.12 -6.88 A 9.9 9.9 0 0 1 8.21 5.54"
          strokeWidth="1.85"
          fill="none"
        />
        <path
          className="tm-dyn-only tm-arc-b2"
          d="M 8.58 -7.20 A 11.2 11.2 0 0 1 9.70 5.60"
          strokeWidth="1.7"
          fill="none"
        />
      </g>
    </>
  );
}

function KnockMicroBody() {
  return (
    <>
      <g opacity="0.92">
        <g strokeWidth="3.6">
          <path d="M 16.0 32.3 C 13.8 38.0, 10.2 41.2, 6.5 40.3" />
          <path d="M 21.0 33.0 C 20.4 39.6, 18.4 44.6, 15.1 47.4" />
          <path d="M 26.0 32.9 C 26.0 40.0, 27.0 45.0, 25.9 49.8" />
          <path d="M 31.0 32.1 C 32.0 39.0, 34.2 44.0, 36.7 47.0" />
        </g>
        <g fill="currentColor" stroke="none">
          <circle cx="6.5" cy="40.3" r="2.7" />
          <circle cx="15.1" cy="47.4" r="2.7" />
          <circle cx="25.9" cy="49.8" r="2.7" />
          <circle cx="36.7" cy="47.0" r="2.7" />
        </g>
      </g>
      <g transform="rotate(-5 25.5 26)">
        <path
          d="M 12.46 29.49 A 13.5 13.5 0 1 1 38.54 29.49 A 35 35 0 0 1 12.46 29.49 Z"
          fill="currentColor"
          fillOpacity="0.18"
          strokeWidth="4.1"
        />
        <g fill="currentColor" stroke="none">
          <circle cx="21.8" cy="25.4" r="2.45" />
          <circle cx="31.6" cy="25.4" r="2.45" />
        </g>
      </g>
      <path
        d="M 36.39 30.05 A 8.9 8.9 0 0 0 53.0 25.6 A 5.5 5.5 0 0 0 47.5 20.1 A 3.4 3.4 0 0 0 44.1 23.5 A 2.1 2.1 0 0 0 46.2 25.6"
        strokeWidth="4.1"
        fill="none"
      />
      <g transform="translate(47.5, 25.6)">
        <path d="M 6.91 -6.67 A 9.6 9.6 0 0 1 7.96 5.37" strokeWidth="2.7" opacity="0.9" />
      </g>
    </>
  );
}

function WaveBody() {
  return (
    <g className="tm-bob-body">
      <g opacity="0.76">
        <g strokeWidth="2.9">
          <path d="M 16.0 32.3 C 13.8 38.0, 10.2 41.2, 6.5 40.3" />
          <path d="M 21.0 33.0 C 20.4 39.6, 18.4 44.6, 15.1 47.4" />
          <path d="M 26.0 32.9 C 26.0 40.0, 27.0 45.0, 25.9 49.8" />
          <path d="M 31.0 32.1 C 32.0 39.0, 34.2 44.0, 36.7 47.0" />
        </g>
        <g fill="currentColor" stroke="none">
          <circle cx="6.5" cy="40.3" r="2.35" />
          <circle cx="15.1" cy="47.4" r="2.35" />
          <circle cx="25.9" cy="49.8" r="2.35" />
          <circle cx="36.7" cy="47.0" r="2.35" />
        </g>
      </g>
      <g transform="rotate(-4 25.5 26)">
        <path
          d="M 12.46 29.49 A 13.5 13.5 0 1 1 38.54 29.49 A 35 35 0 0 1 12.46 29.49 Z"
          fill="currentColor"
          fillOpacity="0.15"
          strokeWidth="3.3"
        />
        <g strokeWidth="2.4" fill="none">
          <path d="M 19.5 26.2 A 2.4 2.4 0 0 1 24.1 26.2" />
          <path d="M 29.3 26.2 A 2.4 2.4 0 0 1 33.9 26.2" />
        </g>
      </g>
      <g className="tm-wave-arm" style={{ transformOrigin: "36.4px 30.2px" }}>
        <path d="M 36.4 30.2 C 44.5 31.2, 48.2 23.5, 51.5 14.8" strokeWidth="3.3" fill="none" />
        <circle cx="51.5" cy="14.8" r="2.45" fill="currentColor" stroke="none" />
      </g>
      <path d="M 55.2 12.5 A 6.5 6.5 0 0 1 57.6 18.8" strokeWidth="1.65" opacity="0.72" />
      <path d="M 45.8 11.4 A 6.5 6.5 0 0 0 44.5 16.0" strokeWidth="1.55" opacity="0.48" />
    </g>
  );
}

function ThinkBody() {
  return (
    <>
      <g opacity="0.76">
        <g strokeWidth="2.9">
          <path d="M 15.2 32.4 C 13.0 38.2, 9.6 41.4, 6.2 40.8" />
          <path d="M 20.0 33.7 C 19.4 40.2, 17.6 45.2, 14.6 48.2" />
          <path d="M 24.8 34.3 C 24.8 41.2, 25.8 46.2, 25.0 51.0" />
          <path d="M 29.8 34.5 C 30.8 41.0, 33.0 45.8, 35.6 48.6" />
        </g>
        <g fill="currentColor" stroke="none">
          <circle cx="6.2" cy="40.8" r="2.3" />
          <circle cx="14.6" cy="48.2" r="2.3" />
          <circle cx="25.0" cy="51.0" r="2.3" />
          <circle cx="35.6" cy="48.6" r="2.3" />
        </g>
      </g>
      <g transform="rotate(5 25 27.5)">
        <path
          d="M 11.96 30.99 A 13.5 13.5 0 1 1 38.04 30.99 A 35 35 0 0 1 11.96 30.99 Z"
          fill="currentColor"
          fillOpacity="0.15"
          strokeWidth="3.3"
        />
        <g fill="currentColor" stroke="none">
          <circle cx="22.8" cy="24.4" r="2.0" />
          <circle cx="32.4" cy="24.4" r="2.0" />
        </g>
      </g>
      <g className="tm-think-arm" style={{ transformOrigin: "33px 34px" }}>
        <path
          d="M 33.0 34.0 C 37.5 39.2, 46.8 38.2, 46.5 31.8 C 46.3 28.2, 43.4 27.4, 40.8 29.2"
          strokeWidth="3.2"
          fill="none"
        />
        <circle cx="40.8" cy="29.2" r="2.2" fill="currentColor" stroke="none" />
      </g>
      {/* 3 Fibonacci ascending thought nodes: r = 1.55 -> 2.4 -> 3.8 */}
      <circle className="tm-think-d1" cx="45.2" cy="21.4" r="1.55" fill="currentColor" stroke="none" />
      <circle className="tm-think-d2" cx="49.8" cy="16.4" r="2.4" fill="currentColor" stroke="none" />
      <circle
        className="tm-think-d3"
        cx="55.2"
        cy="10.6"
        r="3.8"
        fill="currentColor"
        fillOpacity="0.16"
        stroke="currentColor"
        strokeWidth="2.1"
      />
    </>
  );
}

function ReachBody() {
  return (
    <>
      <g opacity="0.72">
        <g strokeWidth="2.85">
          <path d="M 14.0 32.4 C 11.8 37.5, 8.6 40.4, 5.8 39.2" />
          <path d="M 18.6 33.4 C 17.2 39.8, 14.8 44.6, 11.5 47.2" />
          <path d="M 23.6 33.6 C 22.8 40.4, 22.2 45.4, 20.8 49.8" />
          <path d="M 28.6 33.2 C 28.8 39.6, 30.2 44.6, 31.8 48.0" />
        </g>
        <g fill="currentColor" stroke="none">
          <circle cx="5.8" cy="39.2" r="2.25" />
          <circle cx="11.5" cy="47.2" r="2.25" />
          <circle cx="20.8" cy="49.8" r="2.25" />
          <circle cx="31.8" cy="48.0" r="2.25" />
        </g>
      </g>
      <g transform="rotate(4 23.5 27)">
        <path
          d="M 10.46 30.49 A 13.5 13.5 0 1 1 36.54 30.49 A 35 35 0 0 1 10.46 30.49 Z"
          fill="currentColor"
          fillOpacity="0.15"
          strokeWidth="3.3"
        />
        <g fill="currentColor" stroke="none">
          <circle className="tm-eye-blink" cx="20.8" cy="26.2" r="2.05" />
          <circle className="tm-eye-blink" cx="30.2" cy="26.2" r="2.05" />
        </g>
      </g>
      <g className="tm-reach-arm">
        <path d="M 34.2 31.2 C 40.5 34.5, 45.8 24.5, 53.6 27.5" strokeWidth="3.3" fill="none" />
        <circle cx="53.6" cy="27.5" r="2.55" fill="currentColor" stroke="none" />
      </g>
      <g transform="translate(53.6, 27.5)">
        <circle
          cx="0"
          cy="0"
          r="5.6"
          fill="currentColor"
          fillOpacity="0.12"
          stroke="currentColor"
          strokeWidth="1.8"
        />
        <circle
          className="tm-dyn-only tm-reach-ping"
          cx="0"
          cy="0"
          r="6.2"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
        />
      </g>
    </>
  );
}

function SleepBudBody() {
  /* S-3 花苞紧蜷: all 5 tentacles curled snugly around the sleeping round body */
  return (
    <>
      <g className="tm-sleep-body">
        <g opacity="0.76">
          <g strokeWidth="2.9">
            <path d="M 20.6 38.4 C 21.0 43.8, 18.4 47.2, 15.6 45.4" />
            <path d="M 28.0 38.6 C 27.4 44.2, 28.8 48.2, 31.8 46.8" />
            <path d="M 35.4 37.4 C 35.0 42.8, 37.6 46.2, 40.4 44.4" />
          </g>
          <g fill="currentColor" stroke="none">
            <circle cx="15.6" cy="45.4" r="1.95" />
            <circle cx="31.8" cy="46.8" r="1.95" />
            <circle cx="40.4" cy="44.4" r="1.95" />
          </g>
        </g>
        <g transform="rotate(-6 28 31.5)">
          <path
            d="M 14.96 34.99 A 13.5 13.5 0 1 1 41.04 34.99 A 35 35 0 0 1 14.96 34.99 Z"
            fill="currentColor"
            fillOpacity="0.15"
            strokeWidth="3.3"
          />
          <g strokeWidth="2.35" fill="none">
            <path d="M 21.8 31.2 A 2.4 2.4 0 0 0 26.4 31.2" />
            <path d="M 31.4 31.2 A 2.4 2.4 0 0 0 36.0 31.2" />
          </g>
        </g>
        <path d="M 15.2 36.6 C 10.6 40.4, 7.4 35.6, 10.8 31.8" strokeWidth="3.15" fill="none" />
        <circle cx="10.8" cy="31.8" r="1.95" fill="currentColor" stroke="none" />
        <path d="M 40.6 34.2 C 45.2 37.8, 48.2 33.0, 44.8 29.4" strokeWidth="3.15" fill="none" />
        <circle cx="44.8" cy="29.4" r="1.95" fill="currentColor" stroke="none" />
      </g>
      <path
        className="tm-sleep-z1"
        d="M 45.2 17.5 L 49.4 17.5 L 45.2 21.8 L 49.4 21.8"
        strokeWidth="1.9"
        opacity="0.68"
      />
      <path
        className="tm-sleep-z2"
        d="M 51.6 10.2 L 57.2 10.2 L 51.6 15.9 L 57.2 15.9"
        strokeWidth="2.15"
        opacity="0.84"
      />
    </>
  );
}

function CheerBody() {
  return (
    <g className="tm-cheer-body">
      <g opacity="0.74">
        <g strokeWidth="2.9">
          <path d="M 26.2 33.0 C 25.0 39.8, 22.8 45.0, 19.6 48.4" />
          <path d="M 32.0 33.2 C 32.0 40.2, 32.0 45.2, 32.0 50.4" />
          <path d="M 37.8 33.0 C 39.0 39.8, 41.2 45.0, 44.4 48.4" />
        </g>
        <g fill="currentColor" stroke="none">
          <circle cx="19.6" cy="48.4" r="2.35" />
          <circle cx="32.0" cy="50.4" r="2.35" />
          <circle cx="44.4" cy="48.4" r="2.35" />
        </g>
      </g>
      <path
        d="M 18.96 29.99 A 13.5 13.5 0 1 1 45.04 29.99 A 35 35 0 0 1 18.96 29.99 Z"
        fill="currentColor"
        fillOpacity="0.15"
        strokeWidth="3.3"
      />
      <g strokeWidth="2.4" fill="none">
        <path d="M 24.9 26.4 A 2.4 2.4 0 0 1 29.5 26.4" />
        <path d="M 34.5 26.4 A 2.4 2.4 0 0 1 39.1 26.4" />
      </g>
      <g className="tm-cheer-arm-l" style={{ transformOrigin: "21px 31.4px" }}>
        <path d="M 21.0 31.4 C 14.5 32.0, 10.0 26.2, 9.8 18.5" strokeWidth="3.3" fill="none" />
        <circle cx="9.8" cy="18.5" r="2.4" fill="currentColor" stroke="none" />
      </g>
      <g className="tm-cheer-arm-r" style={{ transformOrigin: "43px 31.4px" }}>
        <path d="M 43.0 31.4 C 49.5 32.0, 54.0 26.2, 54.2 18.5" strokeWidth="3.3" fill="none" />
        <circle cx="54.2" cy="18.5" r="2.4" fill="currentColor" stroke="none" />
      </g>
      <g className="tm-cheer-sparks" strokeWidth="1.85" opacity="0.8">
        <line x1="25.4" y1="9.8" x2="24.0" y2="6.8" />
        <line x1="32.0" y1="8.8" x2="32.0" y2="5.2" />
        <line x1="38.6" y1="9.8" x2="40.0" y2="6.8" />
      </g>
    </g>
  );
}

export function TentacleMascot({
  pose = "knock",
  size = 24,
  optical = "auto",
  animate = "none",
  triggerKey = 0,
  className,
  ...svgProps
}: TentacleMascotProps) {
  const useMicro = pose === "knock" && (optical === "micro" || (optical === "auto" && size <= 18));
  const effectiveAnimate: TentacleAnimate = useMicro ? "none" : animate;
  const animClass =
    effectiveAnimate === "once"
      ? "tm-once"
      : effectiveAnimate === "loop"
        ? "tm-loop"
        : "";
  const ariaHidden = svgProps["aria-label"] ? undefined : (svgProps["aria-hidden"] ?? true);

  return (
    <svg
      key={effectiveAnimate === "once" ? `${pose}-${triggerKey}` : pose}
      viewBox="0 0 64 64"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={ariaHidden}
      data-mascot-pose={pose}
      data-mascot-optical={useMicro ? "micro" : "master"}
      data-mascot-animate={effectiveAnimate}
      className={cn("tentacle-mascot shrink-0", animClass, className)}
      {...svgProps}
    >
      {pose === "knock" && (useMicro ? <KnockMicroBody /> : <KnockMasterBody />)}
      {pose === "wave" && <WaveBody />}
      {pose === "think" && <ThinkBody />}
      {pose === "reach" && <ReachBody />}
      {pose === "sleep" && <SleepBudBody />}
      {pose === "cheer" && <CheerBody />}
    </svg>
  );
}

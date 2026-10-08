/**
 * CubeLoader — the brand cube drawing itself in a loop. Web port of the CG1
 * app's loader (components/ui/CubeLoader.tsx there): same geometry, same
 * timing, so the wait feels on-brand and consistent across the two products.
 *
 * Usage:
 *   <CubeLoader />              // inline, 64px
 *   <CubeLoader size={24} />    // compact (chat typing indicator)
 *   <CubeLoader size={72} label="Loading…" />
 */
export default function CubeLoader({ size = 64, label }) {
    return (
        <div className="flex flex-col items-center justify-center">
            <svg
                className="cube-loader-svg"
                width={size}
                height={size}
                viewBox="0 0 100 100"
                fill="none"
                aria-label={label || "Loading"}
                role="status"
            >
                {/* Same geometry as the app's CubeMotif (100×100 viewBox). */}
                <polygon
                    className="cube-loader-hex"
                    points="50,2 92,26 92,74 50,98 8,74 8,26"
                    stroke="var(--cube-magenta)"
                    strokeWidth="4"
                    strokeLinejoin="round"
                />
                <line className="cube-loader-l1" x1="50" y1="50" x2="92" y2="26" stroke="var(--cube-magenta)" strokeWidth="4" />
                <line className="cube-loader-l2" x1="50" y1="50" x2="8" y2="26" stroke="var(--cube-magenta)" strokeWidth="4" />
                <line className="cube-loader-l3" x1="50" y1="50" x2="50" y2="98" stroke="var(--cube-magenta)" strokeWidth="4" />
            </svg>
            {label ? (
                <div className="mt-3 text-[11px] font-bold tracking-[1.5px] uppercase text-cube-lav">
                    {label}
                </div>
            ) : null}
        </div>
    );
}

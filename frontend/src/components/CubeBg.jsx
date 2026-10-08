/**
 * Ambient brand background for candidate-facing pages — blurred
 * magenta/blue/cyan blobs + a faint grid. Render once at the top of a `.cube-brand` page;
 * page content should sit inside a `.cube-content` wrapper (z-index above).
 */
export default function CubeBg() {
    return (
        <div className="cube-bg" aria-hidden="true">
            <div className="blob a" />
            <div className="blob b" />
            <div className="blob c" />
            <div className="grid-tex" />
        </div>
    );
}

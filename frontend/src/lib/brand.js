/**
 * Your branding for the app's own screens — edit here.
 *
 * The candidate-facing pages (apply, status, reschedule, refer) show the
 * company name from each pipeline's Recruiter Profile (Settings), which the API
 * returns; BRAND.companyName is only the fallback while that loads or when it
 * is blank. Images live in frontend/public/: replace logo.png and the icon-*.png /
 * favicon-*.png files, and the name in public/manifest.json and public/index.html.
 *
 * Optional build-time overrides (frontend/.env):
 *   REACT_APP_BRAND_NAME     product name in the header and login screen
 *   REACT_APP_COMPANY_NAME   fallback company name on candidate-facing pages
 */
export const BRAND = {
    name: process.env.REACT_APP_BRAND_NAME || "CGRecruit",
    companyName: process.env.REACT_APP_COMPANY_NAME || "Our team",
    logo: "/logo.png",
};

export default BRAND;

const { createProxyMiddleware } = require("http-proxy-middleware");

module.exports = function (app) {
    app.use(
        "/api",
        createProxyMiddleware({
            // Dev server only: where /api calls go. Defaults to a local backend.
            // Never point this at a deployment with real candidate data.
            target: process.env.API_PROXY_TARGET || "http://localhost:8001",
            changeOrigin: true,
            secure: true,
        }),
    );
};

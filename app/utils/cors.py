from starlette.middleware.cors import CORSMiddleware


class DashboardCORSMiddleware(CORSMiddleware):
    """Let the mounted public API apply its own bearer-only CORS policy."""

    async def __call__(self, scope, receive, send):
        if scope.get("path", "").startswith("/api/v1/public/"):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)

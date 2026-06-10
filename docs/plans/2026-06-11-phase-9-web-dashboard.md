# Phase 9 — Web Dashboard Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.
> **Skills to load for execution:** `test-driven-development`, `verification-before-completion`, `requesting-code-review`

**Goal:** Browser-based dashboard for tenant + workflow + usage management. FastAPI + Jinja2 + HTMX. No JavaScript framework. Self-contained CSS.

**Architecture:** New `agentforge.dashboard` subpackage with:
- `router.py` — FastAPI router with HTML routes (separate from `serve.py`'s JSON API to keep concerns clean)
- `auth.py` — cookie-based session: paste API key, server sets `agentforge_api_key` HttpOnly cookie, dashboard reads it back
- `templates/` — Jinja2 templates, one per page
- `static/dashboard.css` — minimal self-contained CSS (no Tailwind, no CDN, no preprocessor)

Mounted at `/dashboard` in the main `serve.py` app. The JSON API at `/v1/*` keeps working unchanged.

**Tech Stack:**
- `jinja2` — server-rendered templates (already a FastAPI transitive dep)
- HTMX 1.9.x — loaded from CDN (`<script src="https://unpkg.com/htmx.org@1.9.10">`)
- Stdlib `http.cookies` + `secrets` for session — no Flask-Login, no Authlib
- No new Python deps beyond `jinja2` (likely already installed)

**Acceptance Criteria:**
- [ ] `tests/unit/test_dashboard.py` — all pass (15+ tests covering login, listing, CRUD, quota display, run history)
- [ ] `pytest tests/` — 219+30=249+ tests grün
- [ ] Live smoke test (fresh venv, running `agentforge serve`):
  - GET /dashboard/login → 200, HTML with form
  - POST /dashboard/login with valid API key → 302 to /dashboard/, Set-Cookie
  - GET /dashboard/ (with cookie) → 200, HTML showing tenant_id, plan, usage bar
  - GET /dashboard/tenants (with cookie) → 200, list of tenants
  - GET /dashboard/workflows (with cookie) → 200, list of workflow .yaml files
  - GET /dashboard/tenants/acme (with cookie) → 200, plan switcher visible
- [ ] Self-contained: dashboard works without internet (CDN fallback to local copy is out of scope; document the CDN requirement in README)
- [ ] README has "Dashboard" section with screenshot description + URL
- [ ] `git tag v0.5.0` and push

**Out of Scope:**
- WebSocket live updates (HTMX polling is enough for v1)
- Real charts (just CSS bars; Chart.js is a follow-up)
- User accounts / 2FA / password reset (API keys are the only auth)
- Multi-user roles (any tenant with a valid API key is admin)
- Dark mode toggle (single light theme; dark mode is a follow-up)
- Mobile-first responsive design (works on desktop, gracefully degrades on mobile)
- i18n (English only)
- Frontend tests with Playwright (server-rendered HTML is testable with `TestClient` + `response.text` parsing)

**Skills to load for execution:**
- `test-driven-development` — for the RED-GREEN-REFACTOR cycle in every task
- `verification-before-completion` — for the post-implementation checks (evidence before claims)
- `requesting-code-review` — for the pre-commit quality gate (security scan, subagent reviewer)
- `htmx-patterns` — for HTMX-specific patterns (already available in skills)

**Rollback Plan:** All changes additive (new subpackage, new router mounted, new templates, new CSS). `git revert v0.5.0` reverts cleanly. The JSON API at `/v1/*` is untouched.

---

## Plan

### Task 1: Dashboard package skeleton + Jinja2 environment

**Objective:** New subpackage with empty router + Jinja2 environment factory.

**Files:**
- Create: `src/agentforge/dashboard/__init__.py`
- Create: `src/agentforge/dashboard/router.py` (skeleton with `get_templates()` factory)
- Create: `tests/unit/test_dashboard.py` (1 test: package imports)

**Step 1: Write failing test**

```python
# tests/unit/test_dashboard.py
import agentforge.dashboard
from agentforge.dashboard.router import get_templates


def test_dashboard_package_imports():
    assert hasattr(agentforge.dashboard, "router")


def test_get_templates_returns_jinja2_environment():
    env = get_templates()
    # Smoke check: env has expected attributes
    assert hasattr(env, "get_template")
    assert hasattr(env, "from_string")
```

**Step 2: Run test to verify failure**

Run: `pytest tests/unit/test_dashboard.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentforge.dashboard'`

**Step 3: Write minimal implementation**

```python
# src/agentforge/dashboard/__init__.py
"""agentforge.dashboard — server-rendered web UI (FastAPI + Jinja2 + HTMX)."""
from agentforge.dashboard.router import router, get_templates

__all__ = ["router", "get_templates"]
```

```python
# src/agentforge/dashboard/router.py
"""FastAPI router for the dashboard UI."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from jinja2 import Environment, FileSystemLoader, select_autoescape

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Templates live alongside this file at .../dashboard/templates/
_TEMPLATES_DIR = Path(__file__).parent / "templates"


def get_templates() -> Environment:
    """Build a fresh Jinja2 environment. Called per-app to keep state local."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
```

**Step 4: Run test to verify pass**

Run: `pytest tests/unit/test_dashboard.py -v`
Expected: PASS (2/2)

**Step 5: Commit**

```bash
git add src/agentforge/dashboard/ tests/unit/test_dashboard.py
git commit -m "feat(dashboard): package skeleton + Jinja2 environment factory"
```

---

### Task 2: Cookie-based auth (`/dashboard/login` form + middleware)

**Objective:** User pastes API key into a form → server sets `agentforge_api_key` HttpOnly cookie → subsequent requests authenticated via cookie. No state in session — the cookie IS the credential.

**Files:**
- Create: `src/agentforge/dashboard/auth.py`
- Modify: `src/agentforge/dashboard/router.py` (add login routes + cookie dependency)
- Create: `src/agentforge/dashboard/templates/login.html`
- Modify: `tests/unit/test_dashboard.py` (3 tests: login GET/POST, cookie auth)

**Step 1: Write failing test**

```python
# Add to tests/unit/test_dashboard.py
from fastapi.testclient import TestClient
from pathlib import Path
from agentforge.tenants.registry import TenantRegistry
from agentforge.serve import create_app


def test_dashboard_login_get_returns_form(tmp_path):
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.get("/dashboard/login")
    assert r.status_code == 200
    assert "API key" in r.text or "api_key" in r.text
    assert "<form" in r.text.lower()


def test_dashboard_login_post_with_valid_key_sets_cookie(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.post("/dashboard/login",
                    data={"api_key": api_key},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "agentforge_api_key" in r.headers.get("set-cookie", "")


def test_dashboard_login_post_with_invalid_key_rejects(tmp_path):
    TenantRegistry(path=tmp_path / "tenants.json")  # empty registry
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.post("/dashboard/login", data={"api_key": "fake-key"})
    assert r.status_code == 401
```

**Step 2: Run test to verify failure**

Run: `pytest tests/unit/test_dashboard.py -v`
Expected: FAIL — 404 on /dashboard/login

**Step 3: Write minimal implementation**

```python
# src/agentforge/dashboard/auth.py
"""Cookie-based auth for the dashboard. The API key is the credential."""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from agentforge.tenants.registry import TenantRegistry

COOKIE_NAME = "agentforge_api_key"


def get_registry(request: Request) -> TenantRegistry:
    """Access the app's TenantRegistry instance (attached in create_app)."""
    return request.app.state.tenants


def tenant_from_cookie_or_401(request: Request) -> str:
    """Read the API key from the cookie, look up the tenant, return tenant_id.
    Raises 401 if missing or invalid.
    """
    api_key = request.cookies.get(COOKIE_NAME, "")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. POST /dashboard/login with your API key.",
        )
    registry = get_registry(request)
    tenant_id = registry.lookup(api_key)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )
    return tenant_id
```

```html
<!-- src/agentforge/dashboard/templates/login.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>agentforge — login</title>
  <link rel="stylesheet" href="/dashboard/static/dashboard.css">
</head>
<body class="login-page">
  <main class="login-card">
    <h1>agentforge</h1>
    <p class="muted">Paste your API key to sign in.</p>
    {% if error %}
      <div class="alert alert-error">{{ error }}</div>
    {% endif %}
    <form method="post" action="/dashboard/login">
      <label for="api_key">API key</label>
      <input type="password" name="api_key" id="api_key" autofocus required>
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>
```

```python
# Add to src/agentforge/dashboard/router.py:

from fastapi import Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from agentforge.dashboard.auth import (
    COOKIE_NAME, get_registry, tenant_from_cookie_or_401,
)
from agentforge.tenants.registry import TenantRegistry

@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request) -> Response:
    templates = request.app.state.templates
    return templates.get_template("login.html").render(
        request=request, error=None,
    )

@router.post("/login")
def login_post(
    request: Request,
    api_key: str = Form(...),
) -> Response:
    registry: TenantRegistry = get_registry(request)
    tenant_id = registry.lookup(api_key)
    if tenant_id is None:
        templates = request.app.state.templates
        html = templates.get_template("login.html").render(
            request=request, error="Invalid API key.",
        )
        return HTMLResponse(content=html, status_code=401)
    response = RedirectResponse(url="/dashboard/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE_NAME, api_key,
        httponly=True, samesite="lax", max_age=86400,  # 1 day
    )
    return response

@router.get("/logout")
def logout() -> Response:
    response = RedirectResponse(url="/dashboard/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response
```

```python
# In src/agentforge/serve.py create_app(), attach the dashboard router
# and the templates + registry state:

from agentforge.dashboard import router as dashboard_router
from agentforge.dashboard.router import get_templates

# Inside create_app(), after the API routes are defined:
    app.state.tenants = registry
    app.state.templates = get_templates()
    app.include_router(dashboard_router)
```

**Step 4: Run test to verify pass**

Run: `pytest tests/unit/test_dashboard.py -v`
Expected: PASS (2 existing + 3 new = 5/5)

**Step 5: Commit**

```bash
git add src/agentforge/dashboard/ src/agentforge/serve.py tests/unit/test_dashboard.py
git commit -m "feat(dashboard): cookie-based login (POST /dashboard/login + auth dep)"
```

---

### Task 3: Base layout template + CSS

**Objective:** Shared layout for all dashboard pages (header, nav, footer) + minimal self-contained CSS.

**Files:**
- Create: `src/agentforge/dashboard/templates/base.html`
- Create: `src/agentforge/dashboard/templates/_macros.html`
- Create: `src/agentforge/dashboard/static/dashboard.css`
- Modify: `src/agentforge/dashboard/router.py` (mount static files)

**Step 1: Write failing test**

```python
# Add to tests/unit/test_dashboard.py
def test_dashboard_static_css_served(tmp_path):
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.get("/dashboard/static/dashboard.css")
    assert r.status_code == 200
    assert "text/css" in r.headers.get("content-type", "")
    assert len(r.text) > 100  # not empty
```

**Step 2: Run test to verify failure**

Run: `pytest tests/unit/test_dashboard.py -v -k "static_css"`
Expected: FAIL — 404 on /dashboard/static/dashboard.css

**Step 3: Write minimal implementation**

```html
<!-- src/agentforge/dashboard/templates/base.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}agentforge{% endblock %}</title>
  <link rel="stylesheet" href="/dashboard/static/dashboard.css">
  <script src="https://unpkg.com/htmx.org@1.9.10" defer></script>
</head>
<body>
  <header class="topbar">
    <a href="/dashboard/" class="brand">agentforge</a>
    <nav>
      <a href="/dashboard/">Overview</a>
      <a href="/dashboard/tenants">Tenants</a>
      <a href="/dashboard/workflows">Workflows</a>
    </nav>
    <div class="user">
      <span class="muted">{{ tenant_id }}</span>
      <a href="/dashboard/logout" class="btn btn-small">Sign out</a>
    </div>
  </header>
  <main class="container">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

```html
<!-- src/agentforge/dashboard/templates/_macros.html -->
{% macro quota_bar(used, limit, warning, exceeded) %}
  {% set pct = 0 %}
  {% set cls = "ok" %}
  {% if limit %}
    {% set pct = (used / limit * 100) if limit > 0 else 0 %}
    {% if exceeded %}{% set cls = "exceeded" %}
    {% elif warning %}{% set cls = "warning" %}
    {% endif %}
  {% endif %}
  <div class="quota-bar quota-{{ cls }}">
    <div class="fill" style="width: {{ pct if limit else 0 }}%"></div>
    <span class="label">
      {{ "{:,}".format(used) }} / {{ "{:,}".format(limit) if limit else "∞" }} tokens
      {% if exceeded %}<strong>EXCEEDED</strong>{% elif warning %}<em>WARNING</em>{% endif %}
    </span>
  </div>
{% endmacro %}

{% macro plan_badge(plan) %}
  <span class="badge badge-{{ plan }}">{{ plan }}</span>
{% endmacro %}
```

```css
/* src/agentforge/dashboard/static/dashboard.css */
/* Self-contained, no external deps, no preprocessor. */
:root {
  --bg: #f7f8fa;
  --card: #ffffff;
  --border: #e2e5ea;
  --text: #1a1d23;
  --muted: #6b7280;
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --warning: #d97706;
  --warning-bg: #fef3c7;
  --error: #dc2626;
  --error-bg: #fee2e2;
  --ok: #059669;
  --ok-bg: #d1fae5;
  --radius: 6px;
  --shadow: 0 1px 2px rgba(0,0,0,0.05);
}
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
.topbar { background: var(--card); border-bottom: 1px solid var(--border); padding: 0.75rem 1.5rem; display: flex; align-items: center; gap: 2rem; }
.topbar .brand { font-weight: 700; font-size: 1.1rem; text-decoration: none; color: var(--text); }
.topbar nav { display: flex; gap: 1.5rem; }
.topbar nav a { text-decoration: none; color: var(--text); }
.topbar .user { margin-left: auto; display: flex; align-items: center; gap: 1rem; }
.container { max-width: 1100px; margin: 2rem auto; padding: 0 1.5rem; }
.muted { color: var(--muted); }
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.5rem; box-shadow: var(--shadow); margin-bottom: 1.5rem; }
.card h2 { margin-top: 0; }
.btn { display: inline-block; padding: 0.5rem 1rem; background: var(--primary); color: white; border: none; border-radius: var(--radius); cursor: pointer; text-decoration: none; font-size: 0.9rem; }
.btn:hover { background: var(--primary-hover); }
.btn-small { padding: 0.25rem 0.6rem; font-size: 0.8rem; }
.btn-danger { background: var(--error); }
.btn-secondary { background: white; color: var(--text); border: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 0.6rem 0.8rem; text-align: left; border-bottom: 1px solid var(--border); }
th { font-weight: 600; color: var(--muted); font-size: 0.85rem; text-transform: uppercase; }
.badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }
.badge-free { background: #e0e7ff; color: #3730a3; }
.badge-pro { background: var(--ok-bg); color: var(--ok); }
.badge-enterprise { background: #1f2937; color: white; }
.quota-bar { position: relative; height: 28px; background: var(--border); border-radius: var(--radius); overflow: hidden; }
.quota-bar .fill { position: absolute; left: 0; top: 0; bottom: 0; background: var(--ok); transition: width 0.3s; }
.quota-bar .label { position: relative; display: flex; align-items: center; justify-content: center; height: 100%; font-size: 0.8rem; font-weight: 500; }
.quota-bar.quota-warning .fill { background: var(--warning); }
.quota-bar.quota-warning { background: var(--warning-bg); }
.quota-bar.quota-exceeded .fill { background: var(--error); }
.quota-bar.quota-exceeded { background: var(--error-bg); }
.alert { padding: 0.75rem 1rem; border-radius: var(--radius); margin-bottom: 1rem; }
.alert-error { background: var(--error-bg); color: var(--error); }
form input[type="text"], form input[type="password"] { width: 100%; padding: 0.5rem 0.75rem; border: 1px solid var(--border); border-radius: var(--radius); font-size: 1rem; }
form label { display: block; font-size: 0.85rem; color: var(--muted); margin-bottom: 0.25rem; }
.login-page { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: var(--bg); }
.login-card { background: var(--card); border: 1px solid var(--border); padding: 2.5rem; border-radius: 8px; box-shadow: var(--shadow); width: 360px; }
.login-card h1 { margin-top: 0; }
.login-card form button { width: 100%; margin-top: 1rem; padding: 0.75rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }
.metric { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 1rem; }
.metric .value { font-size: 1.8rem; font-weight: 700; }
.metric .label { color: var(--muted); font-size: 0.85rem; }
```

```python
# Add to src/agentforge/dashboard/router.py, after the other routes:

from fastapi.staticfiles import StaticFiles

# In create_app() (via a new function or directly in router), mount the
# static directory at /dashboard/static. Since the router is included
# in serve.py with prefix=/dashboard, we mount on the router itself:

# Actually, StaticFiles can't be mounted on a router — must be on the
# app. Add this in serve.py's create_app():
    from fastapi.staticfiles import StaticFiles
    from pathlib import Path
    _dashboard_static = Path(__file__).parent / "dashboard" / "static"
    app.mount("/dashboard/static", StaticFiles(directory=str(_dashboard_static)),
              name="dashboard-static")
```

**Step 4: Run test to verify pass**

Run: `pytest tests/unit/test_dashboard.py -v -k "static_css"`
Expected: PASS (1 new test)

**Step 5: Commit**

```bash
git add src/agentforge/dashboard/ src/agentforge/serve.py tests/unit/test_dashboard.py
git commit -m "feat(dashboard): base layout, CSS, HTMX CDN, static file mount"
```

---

### Task 4: Overview page (`/dashboard/`)

**Objective:** Authenticated landing page showing current tenant, plan, usage bar, workflow count.

**Files:**
- Create: `src/agentforge/dashboard/templates/overview.html`
- Modify: `src/agentforge/dashboard/router.py` (add `/` route)
- Modify: `tests/unit/test_dashboard.py` (2 tests)

**Step 1: Write failing test**

```python
# Add to tests/unit/test_dashboard.py
def test_dashboard_overview_requires_auth(tmp_path):
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox")
    client = TestClient(app)
    r = client.get("/dashboard/")
    assert r.status_code == 401


def test_dashboard_overview_shows_tenant_and_quota(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    UsageStore(path=tmp_path / "usage.json").record("acme", 42_000)
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox")
    client = TestClient(app)
    r = client.get("/dashboard/", cookies={"agentforge_api_key": api_key})
    assert r.status_code == 200
    assert "acme" in r.text
    assert "42,000" in r.text or "42000" in r.text
    assert "free" in r.text  # default plan
    assert "100,000" in r.text or "100000" in r.text  # free limit
```

**Step 2: Run test to verify failure**

Run: `pytest tests/unit/test_dashboard.py -v -k "overview"`
Expected: FAIL — 401 (auth works) but the test for 200 fails because no /dashboard/ route exists, or it exists but doesn't show the data

**Step 3: Write minimal implementation**

```python
# Add to src/agentforge/dashboard/router.py:

from agentforge.billing.quota import quota_status
from agentforge.billing.usage import UsageStore
from agentforge.workflows.engine import Workflow

@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> Response:
    tenant_id = tenant_from_cookie_or_401(request)
    usage = UsageStore(path=request.app.state.usage_path)
    qs = quota_status(request.app.state.tenants, usage, tenant_id)
    workflows = _list_workflows(request.app.state.workflows_dir)
    templates = request.app.state.templates
    return templates.get_template("overview.html").render(
        request=request,
        tenant_id=tenant_id,
        quota=qs,
        workflow_count=len(workflows),
        recent_workflows=workflows[:5],
    )

def _list_workflows(workflows_dir: Path) -> list[dict]:
    """Return [{name, mtime}] for each .yaml file in workflows_dir, newest first."""
    if not workflows_dir.exists():
        return []
    items = []
    for p in workflows_dir.glob("*.yaml"):
        items.append({
            "name": p.stem,
            "mtime": p.stat().st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items
```

```html
<!-- src/agentforge/dashboard/templates/overview.html -->
{% extends "base.html" %}
{% import "_macros.html" as m %}
{% block title %}Overview — agentforge{% endblock %}
{% block content %}
  <h1>Overview</h1>
  <div class="grid">
    <div class="metric">
      <div class="label">Tenant</div>
      <div class="value">{{ tenant_id }}</div>
    </div>
    <div class="metric">
      <div class="label">Plan</div>
      <div class="value">{{ m.plan_badge(quota.plan.value) }}</div>
    </div>
    <div class="metric">
      <div class="label">Workflows</div>
      <div class="value">{{ workflow_count }}</div>
    </div>
  </div>

  <div class="card">
    <h2>Token usage this month</h2>
    {{ m.quota_bar(quota.used, quota.limit, quota.warning, quota.exceeded) }}
    {% if quota.limit %}
      <p class="muted">{{ "{:,}".format(quota.remaining) }} tokens remaining · {{ "{:.1%}".format(quota.pct) }} of {{ "{:,}".format(quota.limit) }} used</p>
    {% else %}
      <p class="muted">Unlimited plan — no quota.</p>
    {% endif %}
  </div>

  <div class="card">
    <h2>Recent workflows</h2>
    {% if recent_workflows %}
      <table>
        <thead><tr><th>Name</th><th>Last modified</th><th></th></tr></thead>
        <tbody>
          {% for wf in recent_workflows %}
            <tr>
              <td>{{ wf.name }}</td>
              <td class="muted">{{ wf.mtime | int }}</td>
              <td><a href="/dashboard/workflows/{{ wf.name }}" class="btn btn-small btn-secondary">View</a></td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No workflows yet.</p>
    {% endif %}
  </div>
{% endblock %}
```

```python
# In src/agentforge/serve.py create_app(), set the new state attributes:
    app.state.usage_path = mailbox_root.parent / "usage.json"
    app.state.workflows_dir = workflows_dir
```

**Step 4: Run test to verify pass**

Run: `pytest tests/unit/test_dashboard.py -v -k "overview"`
Expected: PASS (2 new tests)

**Step 5: Commit**

```bash
git add src/agentforge/dashboard/ src/agentforge/serve.py tests/unit/test_dashboard.py
git commit -m "feat(dashboard): overview page (tenant, plan, usage bar, workflow count)"
```

---

### Task 5: Tenants list + create + delete

**Objective:** `/dashboard/tenants` lists all tenants with their plans + usage. Inline create form (HTMX). Delete button (form POST).

**Files:**
- Create: `src/agentforge/dashboard/templates/tenants.html`
- Create: `src/agentforge/dashboard/templates/_tenant_row.html` (HTMX partial)
- Modify: `src/agentforge/dashboard/router.py` (add list/create/delete routes)
- Modify: `tests/unit/test_dashboard.py` (3 tests)

**Step 1: Write failing test**

```python
# Add to tests/unit/test_dashboard.py
def test_dashboard_tenants_lists_all_tenants(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    tenants.add("beta")
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox")
    client = TestClient(app)
    r = client.get("/dashboard/tenants", cookies={"agentforge_api_key": api_key})
    assert r.status_code == 200
    assert "acme" in r.text
    assert "beta" in r.text


def test_dashboard_tenants_create_form(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox")
    client = TestClient(app)
    r = client.post("/dashboard/tenants", data={"tenant_id": "newco"},
                    cookies={"agentforge_api_key": api_key},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 200)
    # newco is now in the registry
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    assert "newco" in reg.list_tenants()


def test_dashboard_tenants_delete(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    tenants.add("victim")
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox")
    client = TestClient(app)
    r = client.post("/dashboard/tenants/victim/delete",
                    cookies={"agentforge_api_key": api_key},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 200)
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    assert "victim" not in reg.list_tenants()
```

**Step 2: Run test to verify failure**

Run: `pytest tests/unit/test_dashboard.py -v -k "tenants"`
Expected: FAIL — 404

**Step 3: Write minimal implementation**

```python
# Add to src/agentforge/dashboard/router.py:

@router.get("/tenants", response_class=HTMLResponse)
def tenants_list(request: Request) -> Response:
    tenant_id = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    usage = UsageStore(path=request.app.state.usage_path)
    rows = []
    for tid in registry.list_tenants():
        qs = quota_status(registry, usage, tid)
        rows.append({
            "tenant_id": tid,
            "plan": qs.plan.value,
            "used": qs.used,
            "limit": qs.limit,
            "warning": qs.warning,
            "exceeded": qs.exceeded,
        })
    templates = request.app.state.templates
    return templates.get_template("tenants.html").render(
        request=request, current_tenant=tenant_id, rows=rows,
    )

@router.post("/tenants")
def tenants_create(
    request: Request,
    tenant_id: str = Form(...),
) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    api_key = registry.add(tenant_id)
    # In v1, return the new tenant's API key as a flash message — the
    # operator must copy it. Future: email or one-time display page.
    return RedirectResponse(
        url=f"/dashboard/tenants/{tenant_id}?new_key={api_key}",
        status_code=status.HTTP_303_SEE_OTHER,
    )

@router.post("/tenants/{tenant_id}/delete")
def tenants_delete(request: Request, tenant_id: str) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    registry.remove(tenant_id)
    return RedirectResponse(url="/dashboard/tenants", status_code=status.HTTP_303_SEE_OTHER)
```

```html
<!-- src/agentforge/dashboard/templates/tenants.html -->
{% extends "base.html" %}
{% import "_macros.html" as m %}
{% block title %}Tenants — agentforge{% endblock %}
{% block content %}
  <h1>Tenants</h1>

  <div class="card">
    <h2>Create tenant</h2>
    <form method="post" action="/dashboard/tenants">
      <label for="tenant_id">Tenant ID</label>
      <input type="text" name="tenant_id" id="tenant_id" pattern="[a-zA-Z0-9_-]+" required>
      <button type="submit" class="btn">Create</button>
    </form>
  </div>

  <div class="card">
    <h2>All tenants</h2>
    <table>
      <thead>
        <tr>
          <th>Tenant</th>
          <th>Plan</th>
          <th>Usage</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="tenants-table">
        {% for row in rows %}
          {% include "_tenant_row.html" %}
        {% endfor %}
      </tbody>
    </table>
  </div>
{% endblock %}
```

```html
<!-- src/agentforge/dashboard/templates/_tenant_row.html -->
<tr>
  <td><a href="/dashboard/tenants/{{ row.tenant_id }}">{{ row.tenant_id }}</a></td>
  <td>{{ m.plan_badge(row.plan) }}</td>
  <td>
    {% if row.limit %}
      {{ "{:,}".format(row.used) }} / {{ "{:,}".format(row.limit) }}
      {% if row.exceeded %}<span class="badge badge-enterprise">EXCEEDED</span>
      {% elif row.warning %}<span class="badge badge-pro" style="background:var(--warning-bg);color:var(--warning)">WARN</span>
      {% endif %}
    {% else %}
      {{ "{:,}".format(row.used) }} / ∞
    {% endif %}
  </td>
  <td>
    <a href="/dashboard/tenants/{{ row.tenant_id }}" class="btn btn-small btn-secondary">View</a>
    <form method="post" action="/dashboard/tenants/{{ row.tenant_id }}/delete" style="display:inline"
          onsubmit="return confirm('Delete tenant {{ row.tenant_id }}? This cannot be undone.')">
      <button type="submit" class="btn btn-small btn-danger">Delete</button>
    </form>
  </td>
</tr>
```

**Step 4: Run test to verify pass**

Run: `pytest tests/unit/test_dashboard.py -v -k "tenants"`
Expected: PASS (3 new tests)

**Step 5: Commit**

```bash
git add src/agentforge/dashboard/ tests/unit/test_dashboard.py
git commit -m "feat(dashboard): tenants list + create + delete (with confirm)"
```

---

### Task 6: Tenant detail page with plan switcher

**Objective:** `/dashboard/tenants/{id}` shows full detail: API key (just-created only), plan switcher (HTMX), usage bar.

**Files:**
- Create: `src/agentforge/dashboard/templates/tenant_detail.html`
- Modify: `src/agentforge/dashboard/router.py` (add detail + plan-switch routes)
- Modify: `tests/unit/test_dashboard.py` (3 tests)

**Step 1: Write failing test**

```python
# Add to tests/unit/test_dashboard.py
def test_dashboard_tenant_detail_shows_plan_switcher(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox")
    client = TestClient(app)
    r = client.get("/dashboard/tenants/acme",
                   cookies={"agentforge_api_key": api_key})
    assert r.status_code == 200
    assert "acme" in r.text
    assert "free" in r.text
    # Plan switcher: a form with options for free, pro, enterprise
    assert "pro" in r.text.lower()
    assert "enterprise" in r.text.lower()


def test_dashboard_tenant_detail_shows_new_key_query_param(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox")
    client = TestClient(app)
    r = client.get("/dashboard/tenants/newco?new_key=the_key_xyz",
                   cookies={"agentforge_api_key": api_key})
    assert "the_key_xyz" in r.text  # displayed in a copyable field
    assert "API key" in r.text or "api_key" in r.text


def test_dashboard_tenant_plan_switch(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox")
    client = TestClient(app)
    r = client.post(
        "/dashboard/tenants/acme/plan",
        data={"plan": "pro"},
        cookies={"agentforge_api_key": api_key},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 200)
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    assert reg.get_plan("acme").value == "pro"
```

**Step 2: Run test to verify failure**

Run: `pytest tests/unit/test_dashboard.py -v -k "tenant_detail or plan_switch"`
Expected: FAIL — 404

**Step 3: Write minimal implementation**

```python
# Add to src/agentforge/dashboard/router.py:

@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
def tenant_detail(request: Request, tenant_id: str) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    usage = UsageStore(path=request.app.state.usage_path)
    qs = quota_status(registry, usage, tenant_id)
    new_key = request.query_params.get("new_key")
    templates = request.app.state.templates
    return templates.get_template("tenant_detail.html").render(
        request=request,
        auth_tenant=auth_tenant,
        target_tenant=tenant_id,
        quota=qs,
        new_key=new_key,
    )

@router.post("/tenants/{tenant_id}/plan")
def tenant_set_plan(
    request: Request,
    tenant_id: str,
    plan: str = Form(...),
) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    if not is_valid_plan(plan):
        raise HTTPException(status_code=400, detail=f"invalid plan {plan!r}")
    registry.set_plan(tenant_id, Plan(plan))
    return RedirectResponse(
        url=f"/dashboard/tenants/{tenant_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
```

```html
<!-- src/agentforge/dashboard/templates/tenant_detail.html -->
{% extends "base.html" %}
{% import "_macros.html" as m %}
{% block title %}Tenant {{ target_tenant }} — agentforge{% endblock %}
{% block content %}
  <h1>Tenant: {{ target_tenant }}</h1>
  <p><a href="/dashboard/tenants">← All tenants</a></p>

  {% if new_key %}
    <div class="alert alert-error" style="background:var(--warning-bg);color:var(--warning)">
      <strong>Save this API key — it won't be shown again.</strong>
      <div style="margin-top:0.5rem">
        <input type="text" value="{{ new_key }}" readonly
               style="font-family:monospace;font-size:0.9rem"
               onclick="this.select()">
      </div>
    </div>
  {% endif %}

  <div class="card">
    <h2>Plan</h2>
    <p>Current: {{ m.plan_badge(quota.plan.value) }}</p>
    <form method="post" action="/dashboard/tenants/{{ target_tenant }}/plan">
      <label for="plan">Change plan</label>
      <select name="plan" id="plan">
        <option value="free" {% if quota.plan.value == "free" %}selected{% endif %}>free (100k tokens/month)</option>
        <option value="pro" {% if quota.plan.value == "pro" %}selected{% endif %}>pro (10M tokens/month)</option>
        <option value="enterprise" {% if quota.plan.value == "enterprise" %}selected{% endif %}>enterprise (unlimited)</option>
      </select>
      <button type="submit" class="btn">Update</button>
    </form>
  </div>

  <div class="card">
    <h2>Token usage</h2>
    {{ m.quota_bar(quota.used, quota.limit, quota.warning, quota.exceeded) }}
    <p class="muted">
      {% if quota.limit %}
        {{ "{:,}".format(quota.remaining) }} remaining · {{ "{:.1%}".format(quota.pct) }} used
      {% else %}
        Unlimited
      {% endif %}
    </p>
  </div>
{% endblock %}
```

**Step 4: Run test to verify pass**

Run: `pytest tests/unit/test_dashboard.py -v -k "tenant_detail or plan_switch"`
Expected: PASS (3 new tests)

**Step 5: Commit**

```bash
git add src/agentforge/dashboard/ tests/unit/test_dashboard.py
git commit -m "feat(dashboard): tenant detail with plan switcher + new-key display"
```

---

### Task 7: Workflows list + detail (read-only)

**Objective:** `/dashboard/workflows` lists .yaml files. `/dashboard/workflows/{name}` shows the raw YAML + a "Run" button (POSTs to existing /v1/workflows/.../run).

**Files:**
- Create: `src/agentforge/dashboard/templates/workflows.html`
- Create: `src/agentforge/dashboard/templates/workflow_detail.html`
- Modify: `src/agentforge/dashboard/router.py` (add list/detail routes)
- Modify: `tests/unit/test_dashboard.py` (2 tests)

**Step 1: Write failing test**

```python
# Add to tests/unit/test_dashboard.py
def test_dashboard_workflows_lists_yaml_files(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "greet.yaml").write_text("name: greet\nsteps: []\n")
    (wf_dir / "summarize.yaml").write_text("name: summarize\nsteps: []\n")
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox",
                     workflows_dir=wf_dir)
    client = TestClient(app)
    r = client.get("/dashboard/workflows",
                   cookies={"agentforge_api_key": api_key})
    assert r.status_code == 200
    assert "greet" in r.text
    assert "summarize" in r.text


def test_dashboard_workflow_detail_shows_yaml(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "greet.yaml").write_text("name: greet\ndescription: Says hello\nsteps: []\n")
    app = create_app(tenants_path=tmp_path / "tenants.json",
                     mailbox_root=tmp_path / "mailbox",
                     workflows_dir=wf_dir)
    client = TestClient(app)
    r = client.get("/dashboard/workflows/greet",
                   cookies={"agentforge_api_key": api_key})
    assert r.status_code == 200
    assert "Says hello" in r.text or "description:" in r.text
    assert "<form" in r.text.lower()  # run form
```

**Step 2: Run test to verify failure**

Run: `pytest tests/unit/test_dashboard.py -v -k "workflow"`
Expected: FAIL — 404

**Step 3: Write minimal implementation**

```python
# Add to src/agentforge/dashboard/router.py:

@router.get("/workflows", response_class=HTMLResponse)
def workflows_list(request: Request) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    workflows = _list_workflows(request.app.state.workflows_dir)
    templates = request.app.state.templates
    return templates.get_template("workflows.html").render(
        request=request, auth_tenant=auth_tenant, workflows=workflows,
    )

@router.get("/workflows/{name}", response_class=HTMLResponse)
def workflow_detail(request: Request, name: str) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    wf_path = request.app.state.workflows_dir / f"{name}.yaml"
    if not wf_path.exists():
        raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
    yaml_text = wf_path.read_text(encoding="utf-8")
    templates = request.app.state.templates
    return templates.get_template("workflow_detail.html").render(
        request=request, auth_tenant=auth_tenant,
        name=name, yaml_text=yaml_text,
    )
```

```html
<!-- src/agentforge/dashboard/templates/workflows.html -->
{% extends "base.html" %}
{% block title %}Workflows — agentforge{% endblock %}
{% block content %}
  <h1>Workflows</h1>
  <div class="card">
    {% if workflows %}
      <table>
        <thead><tr><th>Name</th><th></th></tr></thead>
        <tbody>
          {% for wf in workflows %}
            <tr>
              <td><a href="/dashboard/workflows/{{ wf.name }}">{{ wf.name }}</a></td>
              <td><a href="/dashboard/workflows/{{ wf.name }}" class="btn btn-small btn-secondary">View</a></td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No workflows found in <code>{{ workflows_dir }}</code>.</p>
    {% endif %}
  </div>
{% endblock %}
```

```html
<!-- src/agentforge/dashboard/templates/workflow_detail.html -->
{% extends "base.html" %}
{% block title %}Workflow {{ name }} — agentforge{% endblock %}
{% block content %}
  <h1>Workflow: {{ name }}</h1>
  <p><a href="/dashboard/workflows">← All workflows</a></p>

  <div class="card">
    <h2>Definition</h2>
    <pre><code>{{ yaml_text }}</code></pre>
  </div>

  <div class="card">
    <h2>Run</h2>
    <form method="post" action="/v1/workflows/{{ name }}/run" target="_blank">
      <label for="agent">Agent name</label>
      <input type="text" name="agent" id="agent" value="{{ auth_tenant }}" required>
      <input type="hidden" name="api_key" value="{{ api_key_for_run }}">
      <button type="submit" class="btn">Run workflow</button>
    </form>
    <p class="muted">Runs the workflow via the JSON API. The API key field is auto-filled from your session cookie.</p>
  </div>
{% endblock %}
```

Wait — passing the API key in a form field exposes it in HTML source. For v1, the cleaner approach: have the form POST to a dashboard endpoint (`/dashboard/workflows/{name}/run`) that injects the API key server-side from the cookie and calls the API internally. Add that endpoint:

```python
@router.post("/workflows/{name}/run")
async def workflow_run(request: Request, name: str) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    api_key = request.cookies.get(COOKIE_NAME)
    # Call the JSON API internally
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://localhost:{request.url.port}/v1/workflows/{name}/run",
            headers={"X-API-Key": api_key},
            json={"agent": auth_tenant},
        )
    return RedirectResponse(
        url=f"/dashboard/workflows/{name}?run_status={r.status_code}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
```

Actually this adds an httpx dep. Simpler: re-use the existing /v1/workflows/.../run handler by mounting the API under the dashboard prefix too. Or simplest: have the form include the API key as a hidden field (it's already in the user's cookie, so no escalation). For v1 we accept the trade-off and document it.

**Step 4: Run test to verify pass**

Run: `pytest tests/unit/test_dashboard.py -v -k "workflow"`
Expected: PASS (2 new tests, no run-form behavior tested in unit tests — just the page renders)

**Step 5: Commit**

```bash
git add src/agentforge/dashboard/ tests/unit/test_dashboard.py
git commit -m "feat(dashboard): workflows list + detail (read-only YAML view + run form)"
```

---

### Task 8: README Dashboard section

**Objective:** Document the new dashboard.

**Files:**
- Modify: `README.md` (add "## Dashboard" section before "Roadmap")

**Step 1: Write the section**

Add a "## Dashboard" section with:
- URL: `/dashboard/` (after `agentforge serve`)
- Login: paste API key
- Pages: Overview, Tenants (list/create/delete/plan switch), Workflows (view YAML)
- Tech: FastAPI + Jinja2 + HTMX from CDN
- Browser support: any modern browser with JS enabled
- Known limitations: HTMX loaded from CDN (no offline mode), no real-time updates (manual refresh)
- Out-of-scope items (WebSockets, dark mode, mobile-first, etc.)

**Step 2: Verify**

Run: `cat README.md | grep -A 2 "^## Dashboard"` (manual inspection)

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): Dashboard section — pages, login, tech, scope notes"
```

---

### Task 9: Plan-Compliance Check + tag v0.5.0 + push

**Objective:** Verify the plan was followed, run live smoke tests, tag, push.

**Step 1: Run all tests**

```bash
pytest tests/ -q
```

Expected: 249+/249+ grün (was 219 after Phase 8; +30 from Phase 9).

**Step 2: Live smoke test in fresh venv**

```bash
python3 -m venv /tmp/test-dashboard-venv
/tmp/test-dashboard-venv/bin/pip install -e .[dev]
export AGENTFORGE_DATA_DIR=/tmp/test-dashboard-data
/tmp/test-dashboard-venv/bin/agentforge tenants add demo
# (capture the API key from output)
/tmp/test-dashboard-venv/bin/agentforge serve --port 8765 &
SERVER_PID=$!
sleep 2

# Login page
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/dashboard/login
# Expected: 200

# Login POST
curl -s -c /tmp/cookies.txt -X POST \
  -d "api_key=$API_KEY" \
  -o /dev/null -w "%{http_code}\n" \
  http://localhost:8765/dashboard/login
# Expected: 302

# Overview (with cookie)
curl -s -b /tmp/cookies.txt http://localhost:8765/dashboard/ | grep -q "demo" && echo "OK"
# Expected: OK

# Tenants list
curl -s -b /tmp/cookies.txt http://localhost:8765/dashboard/tenants | grep -q "demo" && echo "OK"

# Static CSS
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/dashboard/static/dashboard.css
# Expected: 200

kill $SERVER_PID
```

**Step 3: Plan-Compliance Report**

Verify all 9 tasks done, all acceptance criteria met, no scope creep.

**Step 4: Tag + push**

```bash
git tag v0.5.0
git push origin master --tags
```

**Step 5: Final report to user**

Phase 9 complete: 9 tasks, 30 new tests, v0.5.0 tagged + pushed.

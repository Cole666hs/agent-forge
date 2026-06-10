"""agentforge.serve — FastAPI HTTP server with API-key auth.

Endpoints:
  GET  /health                       — no auth, returns 200
  GET  /v1/inbox?agent=NAME          — auth, list mailbox inbox
  POST /v1/messages                  — auth, send message
  POST /v1/workflows/{name}/run      — auth, run workflow

Auth: `X-API-Key: <key>` header. The server consults a TenantRegistry
to map keys → tenant_id. All mailbox + state operations are scoped to
that tenant.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from agentforge.core.mailbox import FileMailbox
from agentforge.core.message import Message
from agentforge.tenants.registry import TenantRegistry
from agentforge.workflows.engine import State, Workflow, WorkflowError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    to: str
    content: str = Field(min_length=1)
    intent: str = "respond"


class SendMessageResponse(BaseModel):
    id: str
    to: str
    # `from_` is a reserved-ish field name; serialize as `from` to match
    # the wire-format convention used by Message.to_dict()
    from_: str = Field(serialization_alias="from")
    content: str

    model_config = {"populate_by_name": True}


class InboxResponse(BaseModel):
    messages: list[dict]


class RunWorkflowRequest(BaseModel):
    agent: str


class RunWorkflowResponse(BaseModel):
    state_keys: list[str]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    tenants_path: Path,
    mailbox_root: Path,
    state_db: Optional[Path] = None,
    workflows_dir: Optional[Path] = None,
) -> FastAPI:
    """Build the FastAPI app. No IO at import time — pass all config."""
    tenants_path = Path(tenants_path)
    mailbox_root = Path(mailbox_root)
    state_db = Path(state_db) if state_db is not None else mailbox_root.parent / "state.db"
    workflows_dir = Path(workflows_dir) if workflows_dir is not None else mailbox_root.parent / "workflows"

    app = FastAPI(title="agentforge", version="0.2.0")
    registry = TenantRegistry(path=tenants_path)

    # -- auth dependency ---------------------------------------------------

    def require_tenant(request: Request) -> str:
        """Reads X-API-Key header, returns tenant_id (or raises 401)."""
        api_key = request.headers.get("X-API-Key", "")
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-API-Key header required",
            )
        tenant_id = registry.lookup(api_key)
        if tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid API key",
            )
        return tenant_id

    def mailbox_for(tenant_id: str) -> FileMailbox:
        return FileMailbox(root=mailbox_root, tenant_id=tenant_id)

    # -- routes ------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/inbox", response_model=InboxResponse)
    def list_inbox(
        agent: str,
        tenant_id: str = Depends(require_tenant),
    ) -> InboxResponse:
        mbox = mailbox_for(tenant_id)
        messages = mbox.list_inbox(agent, include_read=False)
        return InboxResponse(
            messages=[m.to_dict() for m in messages],
        )

    @app.post("/v1/messages", response_model=SendMessageResponse,
              status_code=status.HTTP_201_CREATED)
    def send_message(
        body: SendMessageRequest,
        tenant_id: str = Depends(require_tenant),
    ) -> SendMessageResponse:
        mbox = mailbox_for(tenant_id)
        msg = Message(
            from_=tenant_id,
            to=body.to,
            content=body.content,
            intent=body.intent,
        )
        mbox.send(msg)
        return SendMessageResponse(
            id=msg.id, to=msg.to, from_=msg.from_, content=msg.content,
        )
    @app.post("/v1/workflows/{name}/run", response_model=RunWorkflowResponse)
    async def run_workflow(
        name: str,
        body: RunWorkflowRequest,
        tenant_id: str = Depends(require_tenant),
    ) -> RunWorkflowResponse:
        wf_path = workflows_dir / f"{name}.yaml"
        if not wf_path.exists():
            raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
        wf = Workflow.from_yaml(wf_path)
        mbox = mailbox_for(tenant_id)
        state = State(tenant_id=tenant_id)
        # No LLM adapter wired by default — workflows with llm_call steps
        # will fail unless caller configures one. The CLI subcommand
        # `agentforge serve --llm <provider>` does this wiring.
        try:
            await wf.run(state=state, mailbox=mbox, llm=None,
                         agent_name=body.agent, state_db=state_db)
        except WorkflowError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return RunWorkflowResponse(state_keys=sorted(state._data.keys()))

    return app

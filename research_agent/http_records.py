"""Research records, controlled experiments and strategy version HTTP routes."""
from __future__ import annotations

from typing import Annotated
from fastapi import APIRouter, Body, Depends, Request

from . import http_support as http
from .http_support import App, Id, respond
from .reports import render_report

JsonBody = Annotated[dict, Body()]


def require_space(app: App, space_id: Id):
    app.store.space(space_id)


router = APIRouter(prefix="/api/spaces/{space_id}", dependencies=[Depends(require_space)])
RECORDS = "/research-records"


@router.get("/strategies", tags=["Strategies"])
def strategies(app: App, space_id: Id):
    return app.strategies.overview(space_id)


@router.post("/strategies/feedback", status_code=201, tags=["Strategies"])
def feedback(app: App, space_id: Id, body: JsonBody):
    return app.strategies.feedback(space_id, body.get("job_id"), body.get("kind"), body.get("note"))


@router.post("/strategies/propose", status_code=201, tags=["Strategies"])
def propose(app: App, space_id: Id, body: JsonBody):
    return app.strategies.propose(space_id, body.get("feedback_id"))


@router.post("/strategies/activate", tags=["Strategies"])
def activate(app: App, space_id: Id, body: JsonBody):
    return app.strategies.activate(space_id, body.get("version_id"), body.get("evaluation_id"))


@router.post("/strategies/rollback", tags=["Strategies"])
def rollback(app: App, space_id: Id, body: JsonBody):
    return app.strategies.rollback(space_id, body.get("reason", "用户回退"))


@router.get(RECORDS + "/reports", tags=["Reports"])
def reports(app: App, space_id: Id):
    return {"reports": app.records.reports(space_id)}


@router.get(RECORDS + "/reports/{report_id}", tags=["Reports"])
def report(app: App, space_id: Id, report_id: Id):
    item = app.records.report(space_id, report_id)
    for version in item["versions"]:
        version["coverage"] = app.records.coverage(version)
    return item


@router.post(RECORDS + "/reports/{report_id}/versions", status_code=201, tags=["Reports"])
def draft(app: App, space_id: Id, report_id: Id, body: JsonBody):
    item, version_id = app.records.draft(space_id, report_id, body)
    return {"report": item, "version_id": version_id}


@router.get(RECORDS + "/reports/{report_id}/compare", tags=["Reports"])
def compare_reports(app: App, space_id: Id, report_id: Id, left: str = "", right: str = ""):
    return app.records.compare_reports(space_id, report_id, left, right)


@router.patch(RECORDS + "/reports/{report_id}/versions/{version_id}", tags=["Reports"])
def transition(app: App, space_id: Id, report_id: Id, version_id: Id, body: JsonBody):
    return app.records.transition(space_id, report_id, version_id, body)


@router.get(RECORDS + "/reports/{report_id}/versions/{version_id}/report.md", tags=["Reports"])
@router.get(RECORDS + "/reports/{report_id}/versions/{version_id}/report.html", tags=["Reports"])
def report_content(request: Request, app: App, space_id: Id, report_id: Id, version_id: Id):
    content = app.records.report_markdown(space_id, report_id, version_id)
    if request.url.path.endswith(".html"):
        return respond(200, render_report(content, "研究报告 · 版本档案").encode("utf-8"), "text/html")
    return respond(200, content.encode("utf-8"), "text/markdown", "report.md")


@router.get(RECORDS + "/graph", tags=["Research"])
def graph(app: App, space_id: Id):
    return app.records.graph(space_id)


@router.post(RECORDS + "/entities", status_code=201, tags=["Research"])
def entity(app: App, space_id: Id, body: JsonBody):
    return app.records.entity(space_id, body)


@router.post(RECORDS + "/relations", status_code=201, tags=["Research"])
def relation(app: App, space_id: Id, body: JsonBody):
    return app.records.relation(space_id, body)


@router.get(RECORDS + "/ideas", tags=["Research"])
def ideas(app: App, space_id: Id):
    return {"ideas": app.records.ideas(space_id)}


@router.get(RECORDS + "/experiments", tags=["Experiments"])
def experiments(app: App, space_id: Id):
    return {"experiments": app.records.experiments(space_id)}


@router.post(RECORDS + "/experiments", status_code=201, tags=["Experiments"])
def create_experiment(app: App, space_id: Id, body: JsonBody):
    return app.records.save_experiment(space_id, body)


@router.get(RECORDS + "/experiments/{experiment_id}", tags=["Experiments"])
def experiment(app: App, space_id: Id, experiment_id: Id):
    return app.records.experiment(space_id, experiment_id)


@router.post(RECORDS + "/experiments/{experiment_id}/versions", status_code=201, tags=["Experiments"])
def experiment_version(app: App, space_id: Id, experiment_id: Id, body: JsonBody):
    return app.records.save_experiment(space_id, body, experiment_id)


@router.get(RECORDS + "/experiment-comparison", tags=["Experiments"])
def compare_experiments(app: App, space_id: Id, left: str = "", right: str = ""):
    return app.records.compare_experiments(space_id, left, right)


@router.get(RECORDS + "/experiment-versions/{version_id}/handoff", tags=["Experiments"])
def handoff(app: App, space_id: Id, version_id: Id):
    return app.records.handoff(space_id, version_id)


@router.post(RECORDS + "/execution-plans", status_code=201, tags=["Experiments"])
def prepare(app: App, space_id: Id, body: JsonBody):
    return app.experiments.prepare(space_id, body)


@router.post(RECORDS + "/experiment-versions/{version_id}/execute", status_code=202, tags=["Experiments"])
def execute(request: Request, app: App, space_id: Id, version_id: Id, body: JsonBody):
    if not http.local_desktop(request):
        raise PermissionError("代码执行仅支持本机 Windows 工作台")
    return app.experiments.submit(space_id, version_id, body)


@router.get(RECORDS + "/executions", tags=["Experiments"])
def executions(app: App, space_id: Id):
    return {"executions": app.experiments.list(space_id)}


@router.get(RECORDS + "/executions/{execution_id}", tags=["Experiments"])
def execution(app: App, space_id: Id, execution_id: Id):
    return app.experiments.get(space_id, execution_id)


@router.get(RECORDS + "/executions/{execution_id}/artifacts/{name}", tags=["Experiments"])
def artifact(app: App, space_id: Id, execution_id: Id, name: str):
    file = app.experiments.artifact(space_id, execution_id, name)
    return respond(200, file.read_bytes(), "text/plain", file.name)

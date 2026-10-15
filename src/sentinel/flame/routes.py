"""HTTP routes for Flame Mode, under /api/v1/flame. Same authentication as the
rest of the API. There is no route that commands ignition or release hardware;
burn-watch flights are ordinary inspection requests an operator approves."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from ..drone.base import FlightCommandRejected
from .operations import OperationError
from .reports import render_html
from .service import FlameError


class EnabledBody(BaseModel):
    enabled: bool


class ConfigurationBody(BaseModel):
    configuration: Literal["security", "agricultural_monitoring"]


class AccessoryRecordBody(BaseModel):
    kind: Literal["inspection", "maintenance", "repair", "commissioning", "note"]
    note: str = Field(default="", max_length=500)
    measured_mass_g: float | None = None
    cg_checked: bool | None = None
    hover_throttle_pct: float | None = None


class AccessorySimBody(BaseModel):
    action: Literal["attach", "detach", "latch_open", "identity_mismatch", "overheat", "silent", "clear"]


class RecordBody(BaseModel):
    kind: str = Field(max_length=40)
    person: str | None = Field(default=None, max_length=60)
    reference: str | None = Field(default=None, max_length=80)
    issued_by: str | None = Field(default=None, max_length=80)
    expires: str | None = Field(default=None, max_length=10)
    valid_from: str | None = Field(default=None, max_length=10)
    valid_until: str | None = Field(default=None, max_length=10)
    field_id: str | None = Field(default=None, max_length=41)
    conditions: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=300)


class OperationBody(BaseModel):
    field_id: str = Field(max_length=41)
    drone_id: str = Field(default="", max_length=60)
    name: str = Field(default="", max_length=80)


class WeatherBody(BaseModel):
    wind_mps: float
    gust_mps: float | None = None
    wind_from_deg: float | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    note: str = Field(default="", max_length=200)


class TextBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    lat: float | None = None
    lon: float | None = None


class IgnitionBody(BaseModel):
    side: str | None = Field(default=None, max_length=20)
    lat: float | None = None
    lon: float | None = None


class StartBody(BaseModel):
    acknowledge_incomplete: bool = False


class WatchBody(BaseModel):
    side: str = Field(max_length=20)


class ReasonBody(BaseModel):
    reason: str = Field(default="", max_length=300)


class CloseBody(BaseModel):
    result: Literal["no_heat_observed", "heat_remains", "not_inspected"] | None = None
    note: str = Field(default="", max_length=500)


def add_routes(app: FastAPI, service: Any, require_auth: Any) -> None:
    flame = service.flame

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KeyError:
            raise HTTPException(status_code=404, detail="not found") from None
        except FlightCommandRejected as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except (FlameError, OperationError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/v1/flame")
    def overview(_: str = Depends(require_auth)) -> dict[str, Any]:
        return flame.overview()

    @app.put("/api/v1/flame/enabled")
    def set_enabled(body: EnabledBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return flame.set_enabled(body.enabled, actor)

    @app.get("/api/v1/flame/compatibility")
    def compatibility(_: str = Depends(require_auth)) -> dict[str, Any]:
        from ..drone import accessories

        return {"profiles": accessories.public_profiles(), "matrix": accessories.compatibility_matrix(), "airframes": accessories.AIRFRAMES}

    @app.get("/api/v1/flame/accessories/{accessory_id}")
    def accessory(accessory_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.accessory, accessory_id)

    @app.post("/api/v1/flame/accessories/{accessory_id}/records")
    def accessory_record(accessory_id: str, body: AccessoryRecordBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.add_accessory_record, accessory_id, body.model_dump(exclude_none=True), actor)

    @app.post("/api/v1/flame/accessories/{accessory_id}/simulate")
    def accessory_simulate(accessory_id: str, body: AccessorySimBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.simulate_accessory, accessory_id, body.action, actor)

    @app.put("/api/v1/flame/drones/{drone_id}/configuration")
    def configuration(drone_id: str, body: ConfigurationBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.set_configuration, drone_id, body.configuration, actor)

    @app.get("/api/v1/flame/drones/{drone_id}/position")
    def drone_position(drone_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.drone_position, drone_id)

    @app.post("/api/v1/flame/qualifications")
    def add_qualification(body: RecordBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.add_qualification, body.model_dump(exclude_none=True), actor)

    @app.post("/api/v1/flame/authorizations")
    def add_authorization(body: RecordBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.add_authorization, body.model_dump(exclude_none=True), actor)

    @app.delete("/api/v1/flame/records/{record_id}")
    def withdraw(record_id: str, actor: str = Depends(require_auth)) -> dict[str, bool]:
        call(flame.withdraw_record, record_id, actor)
        return {"withdrawn": True}

    @app.post("/api/v1/flame/fields")
    def create_field(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.save_field, None, body, actor)

    @app.get("/api/v1/flame/fields/{field_id}")
    def get_field(field_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.field, field_id)

    @app.put("/api/v1/flame/fields/{field_id}")
    def update_field(field_id: str, body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.save_field, field_id, body, actor)

    @app.delete("/api/v1/flame/fields/{field_id}")
    def delete_field(field_id: str, actor: str = Depends(require_auth)) -> dict[str, bool]:
        call(flame.delete_field, field_id, actor)
        return {"deleted": True}

    @app.get("/api/v1/flame/operations")
    def list_operations(limit: int = 50, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return flame.operations_list(max(1, min(limit, 500)))

    @app.post("/api/v1/flame/operations")
    def create_operation(body: OperationBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.create_operation, body.field_id, actor, drone_id=body.drone_id, name=body.name)

    @app.get("/api/v1/flame/operations/{operation_id}")
    def get_operation(operation_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.operation_view, operation_id)

    @app.patch("/api/v1/flame/operations/{operation_id}")
    def update_operation(operation_id: str, body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        allowed = {"checklist", "prescription", "crew", "equipment"}
        if not body or set(body) - allowed:
            raise HTTPException(status_code=422, detail=f"send one or more of {sorted(allowed)}")
        return call(flame.update_operation, operation_id, body, actor)

    @app.post("/api/v1/flame/operations/{operation_id}/weather")
    def add_weather(operation_id: str, body: WeatherBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.add_weather, operation_id, body.model_dump(exclude_none=True), actor)

    @app.post("/api/v1/flame/operations/{operation_id}/observations")
    def add_observation(operation_id: str, body: TextBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.add_observation, operation_id, body.model_dump(exclude_none=True), actor)

    @app.post("/api/v1/flame/operations/{operation_id}/start")
    def start(operation_id: str, body: StartBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.start_monitoring, operation_id, actor, acknowledge_incomplete=body.acknowledge_incomplete)

    @app.post("/api/v1/flame/operations/{operation_id}/ignition")
    def ignition(operation_id: str, body: IgnitionBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.report_ignition, operation_id, body.model_dump(exclude_none=True), actor)

    @app.post("/api/v1/flame/operations/{operation_id}/watch")
    def watch(operation_id: str, body: WatchBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.request_watch, operation_id, body.side, actor)

    @app.post("/api/v1/flame/operations/{operation_id}/post-burn")
    def post_burn(operation_id: str, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.start_post_burn, operation_id, actor)

    @app.post("/api/v1/flame/operations/{operation_id}/stand-down")
    def stand_down(operation_id: str, body: ReasonBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.stand_down, operation_id, body.reason, actor)

    @app.post("/api/v1/flame/operations/{operation_id}/alert")
    def alert(operation_id: str, body: TextBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.emergency_alert, operation_id, body.text, actor)

    @app.post("/api/v1/flame/operations/{operation_id}/close")
    def close(operation_id: str, body: CloseBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.close_operation, operation_id, body.model_dump(exclude_none=True), actor)

    @app.post("/api/v1/flame/operations/{operation_id}/simulate")
    def simulate(operation_id: str, body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return call(flame.simulate, operation_id, body, actor)

    @app.get("/api/v1/flame/operations/{operation_id}/report")
    def report(operation_id: str, format: Literal["json", "html"] = "json", actor: str = Depends(require_auth)) -> Any:
        data = call(flame.report, operation_id)
        service.runtime.store.audit(actor, "burn_report_exported", {"operation_id": operation_id, "format": format, "fingerprint": data["fingerprint"]})
        if format == "html":
            return Response(render_html(data), media_type="text/html", headers={"Cache-Control": "no-store"})
        return data

"""HTTP routes for household-member recognition, under /api/v1/identity. Same
authentication as the rest of the API.

Nothing here returns a face template, a face crop, or a photo. Enrollment
photos are decoded in memory, turned into templates and dropped. Every
enrollment and removal is audited (who, whom, when; never biometric data)."""

from __future__ import annotations

import base64
import binascii
import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .gallery import CONSENT_STATEMENT, CONSENT_STATEMENT_VERSION, METHODS, RELATIONSHIPS, EnrollmentError
from .settings import IdentitySettings

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MODELS = [
    {"name": "YuNet face detector (2023mar)", "source": "OpenCV Zoo", "licence": "MIT"},
    {"name": "SFace face recognizer (2021dec)", "source": "OpenCV Zoo", "licence": "Apache-2.0",
     "note": "training data not documented by OpenCV Zoo; legal review needed before commercial sale (sources.md)"},
]


class ConsentBody(BaseModel):
    agreed: bool
    given_by: str = Field(min_length=1, max_length=80)
    relationship: Literal["self", "parent_or_guardian"]
    method: Literal["in_person", "written", "digital"]
    statement_version: str = Field(max_length=20)
    note: str = Field(default="", max_length=200)


class MemberBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)
    consent: ConsentBody


class PhotosBody(BaseModel):
    images: list[str] = Field(min_length=1, max_length=10, description="base64-encoded JPEG or PNG photos of the member alone")


class CaptureBody(BaseModel):
    camera_id: str = Field(max_length=64)


def add_routes(app: FastAPI, service: Any, require_auth: Any) -> None:
    runtime = service.runtime
    enrollment: dict[str, Any] = {}

    def enroller() -> Any:
        from .service import Enrollment

        settings = runtime.identity.settings
        if enrollment.get("settings") != settings:
            enrollment.update(settings=settings, helper=Enrollment(settings))
        return enrollment["helper"]

    def member_or_404(member_id: str) -> dict[str, Any]:
        try:
            return runtime.identity_gallery.get(member_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown member") from None

    def add_templates(member_id: str, images: list[Any], actor: str, source: str) -> dict[str, Any]:
        settings: IdentitySettings = runtime.identity.settings
        try:
            templates = enroller().templates(images)
            member = runtime.identity_gallery.add_templates(member_id, templates, settings, int(time.time() * 1000))
        except EnrollmentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except FileNotFoundError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown member") from None
        runtime.store.audit(actor, "identity_photos_enrolled", {"member_id": member_id, "photos": len(images), "source": source})
        ready = member["photos_enrolled"] >= settings.enroll_min_photos
        return member | {"enrollment_complete": ready,
                         "next_step": "" if ready else f"add {settings.enroll_min_photos - member['photos_enrolled']} more photo(s) to finish enrollment"}

    @app.get("/api/v1/identity")
    def identity_status(_: str = Depends(require_auth)) -> dict[str, Any]:
        """Recognition settings, enrolled members (no templates), live subjects, consent statement."""
        settings = runtime.identity.settings
        state = runtime.snapshot_state()["identity"]
        return {
            "enabled": settings.enabled, "settings": settings.public(), "members": runtime.identity_gallery.members(),
            "gallery_error": runtime.identity_gallery.error, "subjects": state["subjects"], "worker": state["worker"],
            "consent": {"statement": CONSENT_STATEMENT, "statement_version": CONSENT_STATEMENT_VERSION,
                        "relationships": list(RELATIONSHIPS), "methods": list(METHODS)},
            "models": MODELS,
            "statuses": {"known": "matched an enrolled household member, consistently",
                         "unknown": "a clear face that matched no enrolled member (not an accusation: visitors, delivery people)",
                         "unverified": "no usable face, outside the recognition zones, or not certain either way"},
        }

    @app.post("/api/v1/identity/members")
    def add_member(body: MemberBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        """Record a household member and their consent. Photos are added separately."""
        try:
            member = runtime.identity_gallery.add_member(body.display_name, body.consent.model_dump(), actor, int(time.time() * 1000))
        except EnrollmentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        consent = member["consent"]
        runtime.store.audit(actor, "identity_member_added", {"member_id": member["member_id"], "display_name": member["display_name"],
                                                             "given_by": consent["given_by"], "relationship": consent["relationship"],
                                                             "method": consent["method"], "statement_version": consent["statement_version"]})
        return member

    @app.post("/api/v1/identity/members/{member_id}/photos")
    def add_photos(member_id: str, body: PhotosBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        """Enroll from photos of the member alone. The photos are not stored."""
        member_or_404(member_id)
        from .faces import decode_image

        images = []
        for index, text in enumerate(body.images):
            try:
                data = base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(status_code=422, detail=f"image {index + 1} is not valid base64") from None
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail=f"image {index + 1} is larger than 8 MB")
            try:
                images.append(decode_image(data))
            except EnrollmentError as error:
                raise HTTPException(status_code=422, detail=f"image {index + 1}: {error}") from None
        return add_templates(member_id, images, actor, "photos")

    @app.post("/api/v1/identity/members/{member_id}/capture")
    def capture(member_id: str, body: CaptureBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        """Enroll from the camera's current (privacy-masked) picture while the member stands in front of it alone."""
        member_or_404(member_id)
        if body.camera_id not in runtime.camera_configs:
            raise HTTPException(status_code=404, detail="unknown camera")
        jpeg = runtime.latest_frame.get(body.camera_id)
        status = runtime.camera_status.get(body.camera_id, {})
        if not jpeg or time.time() * 1000 - int(status.get("last_frame_ms") or 0) > 5000:
            raise HTTPException(status_code=503, detail="no current picture from this camera")
        from .faces import decode_image

        return add_templates(member_id, [decode_image(jpeg)], actor, f"camera:{body.camera_id}")

    @app.delete("/api/v1/identity/members/{member_id}")
    def remove_member(member_id: str, actor: str = Depends(require_auth)) -> dict[str, Any]:
        """Consent withdrawn or member removed: templates deleted now, names scrubbed from stored events."""
        try:
            return runtime.identity_member_removed(member_id, actor)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown member") from None

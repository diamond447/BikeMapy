from django.test import Client, override_settings


def test_pages_preview_allows_public_reads() -> None:
    response = Client().get(
        "/health/live/", headers={"Origin": "https://feature-123.bikemapy.pages.dev"}
    )
    assert response.status_code == 200


def test_exact_trusted_origin_can_receive_credentialed_cors() -> None:
    response = Client().get("/health/live/", headers={"Origin": "http://localhost:5173"})
    assert response["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert response["Access-Control-Allow-Credentials"] == "true"
    assert "X-CSRFToken" in response["Access-Control-Expose-Headers"]


def test_preview_regex_origin_never_receives_credentialed_cors() -> None:
    response = Client().get(
        "/health/live/", headers={"Origin": "https://feature-123.bikemapy.pages.dev"}
    )
    assert response["Access-Control-Allow-Origin"] == "https://feature-123.bikemapy.pages.dev"
    assert "Access-Control-Allow-Credentials" not in response
    assert "Access-Control-Expose-Headers" not in response


def test_pages_preview_cannot_submit_mutations() -> None:
    response = Client().post(
        "/api/v1/routes/00000000-0000-4000-8000-000000000000/reports/",
        data={},
        headers={"Origin": "https://feature-123.bikemapy.pages.dev"},
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "This preview is read-only."


def test_canonical_pages_origin_is_not_treated_as_preview() -> None:
    response = Client().post(
        "/health/live/",
        data={},
        headers={"Origin": "https://bikemapy.pages.dev"},
        content_type="application/json",
    )
    assert response.status_code != 403


def test_suffix_origin_is_not_cors_allowed_or_read_only() -> None:
    response = Client().get(
        "/health/live/",
        headers={"Origin": "https://feature.bikemapy.pages.dev.attacker.test"},
    )
    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response


def test_preview_admin_get_is_blocked() -> None:
    response = Client().get(
        "/admin/",
        headers={"Origin": "https://feature-123.bikemapy.pages.dev"},
    )
    assert response.status_code == 403


def test_preview_regex_is_deployment_scoped() -> None:
    with override_settings(READ_ONLY_PREVIEW_ORIGIN_REGEX=r"https://preview\.example\.test"):
        response = Client().post(
            "/health/live/",
            data={},
            headers={"Origin": "https://feature-123.bikemapy.pages.dev"},
            content_type="application/json",
        )
    assert response.status_code != 403

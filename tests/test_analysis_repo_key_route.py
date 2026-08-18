from main import app


def test_analysis_route_accepts_canonical_repo_key_path():
    route_paths = {route.path for route in app.routes}
    assert "/api/analyze/{id:path}" in route_paths
    assert "/api/analyses/similar/{id:path}" in route_paths


def test_feedback_routes_accept_canonical_repo_keys_with_slashes():
    routes = {route.path: route for route in app.routes}
    assert routes["/api/feedback/{id:path}"].path_regex.fullmatch(
        "/api/feedback/github:fastapi/fastapi"
    )
    assert routes["/api/stack-feedback/{id:path}/tech/{tech_name}/role"].path_regex.fullmatch(
        "/api/stack-feedback/github:fastapi/fastapi/tech/FastAPI/role"
    )
    assert routes["/api/stack-feedback/by-id/{id:path}"].path_regex.fullmatch(
        "/api/stack-feedback/by-id/github:fastapi/fastapi"
    )

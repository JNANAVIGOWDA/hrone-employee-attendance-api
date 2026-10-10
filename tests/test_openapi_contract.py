from app.main import app


OPERATIONS = {
    "/health": {"get": ("health", {"200", "503"})},
    "/employees": {
        "post": ("createEmployee", {"201", "409", "422"}),
        "get": ("listEmployees", {"200", "422"}),
    },
    "/attendance/punch-in": {"post": ("punchIn", {"201", "404", "409", "422"})},
    "/attendance/punch-out": {"post": ("punchOut", {"200", "404", "409", "422"})},
    "/attendance": {"get": ("listAttendance", {"200", "422"})},
    "/attendance/{emp_code}/{date}": {
        "patch": ("regularizeAttendance", {"200", "404", "409", "422"})
    },
    "/analytics/employees/{emp_code}/monthly": {
        "get": ("employeeMonthly", {"200", "404", "422"})
    },
    "/analytics/departments/summary": {"get": ("departmentSummary", {"200", "422"})},
    "/analytics/leaderboard/late": {"get": ("lateLeaderboard", {"200", "422"})},
    "/analytics/departments/{department}/trend": {
        "get": ("departmentTrend", {"200", "404", "422"})
    },
    "/admin/explain/{endpoint}": {"get": ("explainEndpoint", {"200", "422"})},
}

QUERY_PARAMETERS = {
    ("/employees", "get"): {"department", "page", "page_size"},
    ("/attendance", "get"): {
        "emp_code", "date_from", "date_to", "status", "page", "page_size"
    },
    ("/analytics/employees/{emp_code}/monthly", "get"): {"month"},
    ("/analytics/departments/summary", "get"): {"month", "department"},
    ("/analytics/leaderboard/late", "get"): {"month", "limit", "department"},
    ("/analytics/departments/{department}/trend", "get"): {"from", "to"},
    ("/admin/explain/{endpoint}", "get"): {
        "emp_code", "month", "department", "limit", "date_from", "date_to",
        "status", "from", "to", "page", "page_size",
    },
}


def test_openapi_routes_operation_ids_and_status_codes_match_contract():
    schema = app.openapi()
    assert set(schema["paths"]) == set(OPERATIONS)

    for path, methods in OPERATIONS.items():
        for method, (operation_id, status_codes) in methods.items():
            operation = schema["paths"][path][method]
            assert operation["operationId"] == operation_id
            assert set(operation["responses"]) == status_codes


def test_openapi_query_parameters_match_contract():
    schema = app.openapi()

    for path, methods in OPERATIONS.items():
        for method in methods:
            operation = schema["paths"][path][method]
            actual = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "query"
            }
            assert actual == QUERY_PARAMETERS.get((path, method), set())


def test_openapi_success_responses_expose_contract_fields():
    schema = app.openapi()
    response_fields = {
        ("/health", "get"): {"status"},
        ("/employees", "post"): {
            "emp_code", "name", "email", "department", "shift_start",
            "shift_end", "joined_on", "created_at",
        },
        ("/employees", "get"): {"items", "total", "page", "page_size"},
        ("/attendance/punch-in", "post"): {
            "emp_code", "date", "status", "punch_in", "punch_out",
            "work_hours", "late_minutes", "overtime_minutes", "half_day", "history",
        },
        ("/attendance/punch-out", "post"): {
            "emp_code", "date", "status", "punch_in", "punch_out",
            "work_hours", "late_minutes", "overtime_minutes", "half_day", "history",
        },
        ("/attendance", "get"): {"items", "total", "page", "page_size"},
        ("/attendance/{emp_code}/{date}", "patch"): {
            "emp_code", "date", "status", "punch_in", "punch_out",
            "work_hours", "late_minutes", "overtime_minutes", "half_day", "history",
        },
        ("/analytics/employees/{emp_code}/monthly", "get"): {
            "emp_code", "month", "working_days", "present_days", "leave_days",
            "late_count", "total_late_minutes", "total_overtime_minutes", "attendance_pct",
        },
        ("/analytics/departments/summary", "get"): {"month", "items"},
        ("/analytics/leaderboard/late", "get"): {"month", "items"},
        ("/analytics/departments/{department}/trend", "get"): {"department", "items"},
        ("/admin/explain/{endpoint}", "get"): {"endpoint", "collection", "explain"},
    }

    for (path, method), expected_fields in response_fields.items():
        operation = schema["paths"][path][method]
        success_code = "201" if method == "post" and path in {"/employees", "/attendance/punch-in"} else "200"
        response_schema = operation["responses"][success_code]["content"]["application/json"]["schema"]
        component_name = response_schema["$ref"].split("/")[-1]
        actual_fields = set(schema["components"]["schemas"][component_name]["properties"])
        assert actual_fields == expected_fields


def test_openapi_date_and_status_formats_match_contract():
    schema = app.openapi()
    employee_input = schema["components"]["schemas"]["EmployeeIn"]["properties"]
    assert employee_input["joined_on"]["format"] == "date"

    attendance_parameters = schema["paths"]["/attendance"]["get"]["parameters"]
    status_schema = next(parameter["schema"] for parameter in attendance_parameters if parameter["name"] == "status")
    assert status_schema["anyOf"][0]["enum"] == ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

    trend_parameters = schema["paths"]["/analytics/departments/{department}/trend"]["get"]["parameters"]
    from_schema = next(parameter["schema"] for parameter in trend_parameters if parameter["name"] == "from")
    assert from_schema["format"] == "date"

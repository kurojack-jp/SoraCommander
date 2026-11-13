"""Data-only validation of proven no-answer diagnostics; grants no authority."""


def valid_no_answer(result):
    """Accept only C2-C5 terminal tuples and their observation identifiers."""
    if type(result) is not dict:
        return False
    stage = result.get("stage")
    if stage in ("FIRST_GUI", "SECOND_GUI"):
        outcome = "launch_outcome"
    elif stage in ("FILE_GUI", "INPUT_GUI"):
        outcome = "write_outcome"
    else:
        return False
    required = {"status", "stage", "reason", "approval_state", outcome,
                "auto_retry", "required_action"}
    return (required <= result.keys()
            and result.keys() <= required | {"pending_id", "request_id", "session_id"}
            and result["status"] == "TIMED_OUT"
            and result["reason"] in ("EXPIRED", "CLOSED")
            and result["approval_state"] == "EXPIRED"
            and result[outcome] == "NOT_STARTED"
            and result.get("session_id") is None
            and result["auto_retry"] is False
            and result["required_action"] == "REPORT_AND_WAIT"
            and all(type(result[key]) is str and 0 < len(result[key]) <= 128
                    and result[key].isascii()
                    for key in ("pending_id", "request_id") if key in result))

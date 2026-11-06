"""config.py - everything you edit for values: URLs, variables, auth, feature switches, known differences.

Search for FILL_ and REPLACE_ME: those are the values you still have to enter.
Secrets are read from environment variables, never typed in this file.
"""
import os
import random
import uuid
from datetime import date, timedelta


def env(name, default=""):
    return os.getenv(name, default)


def rand_str(n=6):
    return uuid.uuid4().hex[:n]


def bearer(t):
    """Accepts a token with or without the 'Bearer ' prefix."""
    return t if (not t or t.lower().startswith("bearer ")) else "Bearer " + t


# ---------------------------------------------------------------- service URLs
# One full base URL per service and cloud. Include the version path, for example
# https://host/v2 or https://host/pcservices/rest/v7 (copy them from your old AWS_ENV / GCP_ENV).
SERVICES = ["program_mgmt", "codes_dist", "release_mgmt", "campaign_mgmt", "config_mgmt", "access_mgmt",
            "codegen_batch_mgmt", "refdata_mgmt", "codegen_functions", "codegen_realtime_service",
            "codes_mgmt", "audit_mgmt", "real_viral_time_codes_service"]
BASE_URL = {cloud: {s: "https://REPLACE_ME" for s in SERVICES} for cloud in ("aws", "gcp")}
TIMEOUT = 30
VERIFY_SSL = True
OUTPUT_DIR = "runs"

# ---------------------------------------------------------------- variables
# SAME names on both clouds, DIFFERENT values. Paths use :name (or {name}), bodies use {name}.
_RUN = {  # one value per run, shared by AWS and GCP (your EXECUTION_VALUES / EXECUTION_REALTIME)
    "program_start_date": (date.today() + timedelta(days=1)).isoformat(),
    "program_end_date": (date.today() + timedelta(days=365)).isoformat(),
    "program_suffix": rand_str(6), "release_suffix": rand_str(6), "campaign_suffix": rand_str(6),
    "promotion_suffix": rand_str(6), "org_suffix": rand_str(6),
    "consumerId": "consumer-" + rand_str(6), "transactionId": str(uuid.uuid4()),
    # redeem and unredeem calls must use the same ids, so they are fixed for the whole run
    "viralConsumerId": "consumer-" + rand_str(6), "viralTransactionId": str(uuid.uuid4()),
    "pincodeConsumerId": "consumer-" + rand_str(6), "pincodeTransactionId": str(uuid.uuid4()),
    "trxn": lambda: "trxnId%d" % random.randint(1000, 9999),   # a new value on every call
}
_IDS = ["programId", "releaseId", "campaignId", "promotionId", "lotId", "batchId", "batchItemId", "orgId",
        "userId", "ownerGroupId", "partnerId", "roleId", "closureTypeId", "closureColorId",
        "closureDiameterId", "trademarkId", "brandId", "beverageId", "primaryContainerId"]
_CODES = ["realtimeCode", "viralCode", "redeemedpincode", "redeemedprogramId", "redeemedconsumerId",
          "inactivepincode", "inactivepincodeprogramId", "inactiveviralCode", "inactiveviralprogramId",
          "futureviralCode", "futureviralprogramId"]

VARS = {}
for _c in ("aws", "gcp"):
    _P = _c.upper()
    VARS[_c] = {
        **_RUN,
        **{k: "FILL_%s_%s" % (_P, k) for k in _IDS + _CODES},   # replace with the real value for each cloud
        "category": "CONTENT", "type": "TAXANOMY", "default_country": "US",
        "point_type": "REGULAR", "default_codes_allotted": 10000,
        # secrets: set these environment variables (GCP also needs the login cookie)
        "authorization": env(_P + "_AUTHORIZATION"),        # the full value, for example "Basic abc..."
        "mgmt_token": bearer(env(_P + "_MGMT_TOKEN")),
        "cookie": env("GCP_COOKIE") if _c == "gcp" else "",
    }

# What the curl commands in the report show instead of a secret.
SECRET_PLACEHOLDER = {
    "authorization": "Basic BASIC_CREDENTIALS",
    "mgmt_token": "Bearer MGMT_TOKEN",
    "cookie": "GCP_COOKIE",
}

# ---------------------------------------------------------------- auth profiles
# Types: basic | bearer | header | headers | cookie. Each group or op in apis.py names one profile.
AUTH = {
    # realtime and viral services: Basic on AWS; Basic plus the login cookie on GCP
    "realtime": {
        "aws": {"type": "header", "name": "Authorization", "value": "{authorization}"},
        "gcp": {"type": "headers", "headers": {"Authorization": "{authorization}", "Cookie": "{cookie}"}},
    },
    # management services: Bearer token on AWS; Bearer token plus the login cookie on GCP
    "mgmt": {
        "aws": {"type": "header", "name": "Authorization", "value": "{mgmt_token}"},
        "gcp": {"type": "headers", "headers": {"Authorization": "{mgmt_token}", "Cookie": "{cookie}"}},
    },
}

# Records that must already exist. A test that lists needs=["user"] is SKIPPED if this fails on a cloud.
PREREQUISITES = [
    {"name": "user", "host": "access_mgmt", "path": "/users/:userId", "auth": "mgmt"},
]

# ---------------------------------------------------------------- feature switches
FEATURES = {
    "response_time": {"enabled": True, "runs_per_api": 3, "warm_up_call": True,
                      "slow_pct": 30, "slow_ms": 2000},     # repeats apply to GET calls only
    "compare_headers": {"enabled": False, "check": ["content-type", "cache-control",
                        "x-content-type-options", "strict-transport-security"]},   # off, as in your old runner
    "response_size": {"enabled": True, "tolerance_pct": 10},
    "type_checks": {"enabled": True},
    "retry_failed": {"enabled": True},
    "parallel": {"enabled": True},
    "history": {"enabled": True},
}

# Field names whose VALUES are ignored in the comparison (they differ on every call).
IGNORE_FIELDS = ["id", "createdAt", "updatedAt", "timestamp", "value", "traceId"]

# Accepted differences: matching ones show as ACCEPTED instead of FAIL ("expires" is optional).
# Keys: gcp_extra, gcp_missing, value_diff, type_diff, headers.
KNOWN_DIFFERENCES = [
    # {"service": "Get User By Id", "gcp_extra": ["registrationDate"], "reason": "GCP adds a field", "expires": "2027-06-30"},
]

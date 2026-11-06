"""apis.py - every API, grouped. Add a group or an op here; run.py needs no change.

group(category, name, service, "", auth, ops)   service = key in config.BASE_URL; ops run in the order written
op(method, path, name, body=..., cases=[...])   one API call; a positive case is added automatically
neg(name, expect, ...)                          negative case, see below
Paths: :programId and {programId} both work and are filled from config.VARS for each cloud.

op options:   body=dict|text|function(cloud, vars)   capture={"programId": "programId"}   host="release_mgmt"
              static=True (use the fixed IDs from config, not IDs created in this run)   neg_only=True (no positive case)
              tags=[...]   needs=["user"]   overrides={"gcp": {"path": ..., "body": ...}}
neg options:  vars={"programId": "BAD"}   body=...   path="/other/path?x=1"   auth_mode="invalid"|"missing"
              expect_msg="text", match="exact"|"startswith"|"contains"   static=True
Ops tagged "todo" have a placeholder body: paste your real body builder, or skip them with the --tag option.
"""
import string
from config import rand_str


def build_charset(chars):
    """Character list in the shape the code-descriptor API expects (your build_charset / build_aws_charset)."""
    return [{"code": ch, "synonym": "", "checked": True} for ch in chars]


DEFAULT_EXPECT = {"GET": 200, "POST": 201, "PUT": 200, "DELETE": 200}


def op(method, path, name, body=None, cases=(), auth=None, expect=None, capture=None, needs=(), fmt=None,
       overrides=None, tags=(), host=None, static=False, neg_only=False):
    return dict(method=method, path=path, name=name, body=body, cases=list(cases), auth=auth,
                expect=expect or DEFAULT_EXPECT[method], capture=capture or {}, needs=list(needs), fmt=fmt,
                overrides=overrides or {}, tags=list(tags), host=host, static=static, neg_only=neg_only)


def neg(name, expect, vars=None, body=None, auth_mode=None, tags=(), path=None, expect_msg=None,
        match="contains", static=False):
    return dict(name=name, expect=expect, vars=vars or {}, body=body, auth_mode=auth_mode, tags=list(tags),
                path=path, expect_msg=expect_msg, match=match, static=static, type="Negative")


def group(category, name, host, base, auth, ops, fmt="json"):
    for o in ops:
        o["path"] = base + o["path"]
        o["auth"] = o["auth"] or auth
        o["fmt"] = o["fmt"] or fmt
    return dict(category=category, name=name, host=host, ops=ops)


def G(path, name, **kw):
    return op("GET", path, name, **kw)


# ======================================================================= body builders (AWS and GCP differ)
def _dates(v):
    return v["program_start_date"] + "T00:00:00.000Z", v["program_end_date"] + "T00:00:00.000Z"


def program_body(cloud, v):
    s, e, gcp = *_dates(v), cloud == "gcp"
    return {
        "programSummary": {
            "name": ("GCPProgram-" if gcp else "TestProgram-") + v["program_suffix"], "description": "",
            "startDate": s, "endDate": e, "country": v["default_country"], "ownerGroup": v["ownerGroupId"],
            "localStartTimezone": "UTC", "localEndTimezone": "UTC",
            "desc": "test" if gcp else "TestProgram-" + rand_str(6)},
        "codeDescriptor": {
            "profanityCheckEnabled": False, "dictionaryIds": [], "length": 10, "prefix": "" if gcp else "PR",
            "maxCount": 100000, "luhnsChecksumEnabled": False, "pointValue": 0,
            "characterSet": {"numbers": build_charset(string.digits),
                             "upperCaseAlphabets": build_charset(string.ascii_uppercase),
                             "lowerCaseAlphabets": build_charset(string.ascii_lowercase)}},
        "codeRedemptionRules": {
            "validAttemptsPeriodValue": 1, "inValidAttempts": 10, "inValidAttemptsPeriodValue": 10,
            "validAttempts": 25, "validAttemptsPeriod": "CALENDAR_DAY", "inValidAttemptsPeriod": "MINUTE",
            "blockPeriod": "", "blockPeriodValue": ""},
        "members": []}


def program_update_body(cloud, v):
    b = program_body(cloud, v)
    b["programSummary"]["id"] = v["programId"]
    if cloud == "aws":
        b["programSummary"].update(name="UpdatedProgram-" + rand_str(6), desc="UpdatedProgram-" + rand_str(6))
        b["codeDescriptor"]["maxCount"] = v["default_codes_allotted"]
        b["codeRedemptionRules"].update(blockPeriod=None, blockPeriodValue=None)
    return b


def release_body(cloud, v):
    s, e = _dates(v)
    return {"name": ("GCPRelease-" if cloud == "gcp" else "Release-") + rand_str(6), "startDate": s, "endDate": e,
            "realtimeCodesEnabled": False, "testRelease": False, "localActivateTimezone": "UTC",
            "localInactivateTimezone": "UTC", "state": "", "programId": v["programId"]}


def release_update_body(cloud, v):
    s, e = _dates(v)
    b = {"name": "updated-release-" + rand_str(6), "description": "updated release", "programId": v["programId"],
         "startDate": s, "endDate": e, "testRelease": False, "realtimeCodesEnabled": False}
    if cloud == "aws":   # TODO: the top of your AWS update body was not visible; check the other fields
        b["state"] = "CODEGEN_COMPLETE"
    return b


def promotion_body(cloud, v):
    s, e = _dates(v)
    return {"id": "", "name": "Promo-" + rand_str(6), "desc": "Promo-" + rand_str(6), "startDate": s, "endDate": e,
            "programId": v["programId"], "localStartTimezone": "UTC", "localEndTimezone": "UTC"}


def promotion_update_body(cloud, v):
    s, e = _dates(v)
    gcp = cloud == "gcp"
    tz = "UTC" if gcp else "Etc/UTC"
    return {"id": v["promotionId"], "name": "Marketing Campaign - GCP" + rand_str(2) if gcp else "UpdatedPromo-" + rand_str(6),
            "desc": "Test" if gcp else "testdesc", "startDate": s, "endDate": e, "programId": v["programId"],
            "localStartTimezone": tz, "localEndTimezone": tz}


def lot_body(cloud, v):
    gcp = cloud == "gcp"   # TODO: your AWS lot body was not visible; this uses the real ids from config
    p = {k: ("none" if gcp else v.get(k + "Id", "none")) for k in
         ("trademark", "brand", "beverage", "primaryContainer", "closureColor", "closureDiameter", "closureType")}
    names = {k + "Name": "None" for k in p}
    return {"name": "LOT0" + rand_str(6), "codesAlloted": v["default_codes_allotted"], "pointValue": 1,
            "pointType": "REGULAR", "promotion": "{promotionId}",
            "codeRedemptionRules": {"validAttempts": "", "validAttemptsPeriod": "", "validAttemptsPeriodValue": ""},
            "product": {**{k + "Id": x for k, x in p.items()}, **names, "secondaryPackageId": "none",
                        "secondaryPackageName": "None"}}


def batch_body(cloud, v):   # TODO: paste your build_batch_create_body_aws / _gcp (it references {lotId})
    return {"name": "Batch-" + rand_str(6), "lotId": "{lotId}"}


def campaign_body(cloud, v, update=False):
    s, e, gcp = *_dates(v), cloud == "gcp"
    zero = 0 if update else ""
    lim = {"maxRedemptions": zero, "maxRedemptionsPeriod": "", "maxRedemptionsPeriodValue": zero}
    if update and not gcp:
        name, code = "FIFA2030a", "FIFA2030"
    elif gcp:
        name, code = "TEST-" + rand_str(6), "CODE" + rand_str(4).upper()
    else:
        name, code = "FIFA-" + rand_str(5), "FIFA" + rand_str(4).upper()
    return {"summary": {"name": name, "code": code, "startDate": s, "endDate": e,
                        "localActivateTimezone": "UTC", "localInactivateTimezone": "UTC"},
            "details": {"pointType": "REGULAR" if (gcp or update) else "", "pointValue": 1,
                        "promotion": "{promotionId}", "testCampaign": False,
                        "redemptionRules": {"maxNoOfRedemptions": 100000 if update else "100000",
                                            "campaignRedemptionLimit": dict(lim),
                                            "consumerRedemptionLimit": dict(lim)}}}


def campaign_update_body(cloud, v):
    return campaign_body(cloud, v, update=True)


def todo_body(cloud, v):   # TODO: paste your owner group / user / role body builder here
    return {"name": "todo-" + rand_str(6)}


def config_data_body(cloud, v):
    return {"category": "CONTENT", "type": "TAXANOMY", "name": "test-" + rand_str(5),
            "description": "parity config", "active": True}


# ======================================================================= groups
RT = "real_viral_time_codes_service"
GROUPS = [
    # ---------------------------------------------------------------- creation flow (runs first, in order)
    group("Management", "Create flow", "program_mgmt", "", "mgmt", [
        op("POST", "/programs", "Create Program", body=program_body, capture={"programId": "programId"}),
        op("POST", "/programs/:programId/releases", "Create Release", host="release_mgmt", body=release_body,
           capture={"releaseId": "releaseId"}),
        op("POST", "/programs/:programId/promotions", "Create Promotion", body=promotion_body,
           capture={"promotionId": "promotionId"}),   # before Lot: the lot body uses {promotionId}
        op("POST", "/programs/:programId/releases/:releaseId/lots", "Create Lot", host="release_mgmt",
           body=lot_body, capture={"lotId": "lotId"}),
        op("POST", "/programs/:programId/releases/:releaseId/batches", "Create Batch", host="release_mgmt",
           body=batch_body, capture={"batchItemId": "id", "batchId": "batchId"}, tags=["todo"]),
        op("POST", "/programs/:programId/campaigns", "Create Campaign", host="campaign_mgmt", body=campaign_body,
           capture={"campaignId": "campaignId"}),
        # updates: methods and paths below are assumed, check them against your old routes
        op("PUT", "/programs/:programId", "Update Program", body=program_update_body, tags=["verify"]),
        op("PUT", "/programs/:programId/releases/:releaseId", "Update Release", host="release_mgmt",
           body=release_update_body, tags=["verify"]),
        op("PUT", "/programs/:programId/promotions/:promotionId", "Update Promotion", body=promotion_update_body,
           tags=["verify"]),
        op("PUT", "/programs/:programId/campaigns/:campaignId", "Update Campaign", host="campaign_mgmt",
           body=campaign_update_body, tags=["verify"]),
    ]),
    # ---------------------------------------------------------------- standalone creates
    group("Management", "Access: create", "access_mgmt", "", "mgmt", [
        op("POST", "/owner-groups", "Create Owner Group", body=todo_body, tags=["todo"]),
        op("POST", "/users", "Create User", body=todo_body, tags=["todo"]),
        op("POST", "/roles", "Create Role User", body=todo_body, tags=["todo"]),
    ]),
    group("Management", "Config", "config_mgmt", "", "mgmt", [
        G("/conf-data/app", "Get App Config"),
        G("/conf-data/categories/:category/types/:type", "Get Config Data"),
        op("POST", "/conf-data", "Create Config Data", body=config_data_body),
    ]),
    # ---------------------------------------------------------------- GET routes
    group("Management", "Access", "access_mgmt", "", "mgmt", [
        G("/owner-groups", "Get Owner Groups"), G("/roles", "Get Roles"), G("/users", "Get Users"),
        G("/users/:userId", "Get User By Id"), G("/owner-groups/:ownerGroupId/users", "Get Owner Group Users"),
    ]),
    group("Management", "Program", "program_mgmt", "", "mgmt", [
        G("/programs", "Get All Programs"), G("/programs/:programId", "Get Program By Id"),
        G("/promotions", "Get Promotions"), G("/programs/:programId/promotions/:promotionId", "Get Promotions By Id"),
        G("/partners", "Get All Partners"), G("/partners/:partnerId", "Get Partner By Id"),
        G("/closure-types", "Get All Closure Types"), G("/closure-types/:closureTypeId", "Get By Id Closure Types"),
        G("/programs?country=US", "Programs By Country"),
    ]),
    group("Management", "Release", "release_mgmt", "", "mgmt", [
        G("/programs/:programId/releases", "Get All Releases For Program"),
        G("/programs/:programId/releases/:releaseId", "Get Release By Id"),
        G("/programs/:programId/releases/:releaseId/lots", "Get All Lots for Release"),
        G("/programs/:programId/releases/:releaseId/lots/:lotId", "Get Lot By Id"),
        G("/programs/:programId/releases/:releaseId/batches", "Get All Batches for Release"),
        G("/programs/:programId/releases/:releaseId/batches/:batchId", "Get Batch By Id"),
        G("/programs/:programId/releases/:releaseId/batchItems", "Get Batch Items"),
    ]),
    group("Management", "Campaign", "campaign_mgmt", "", "mgmt", [
        G("/campaigns", "Get All Campaigns"), G("/campaigns/:campaignId", "Get Campaign By Id"),
        G("/campaigns?programId=:programId", "Get Campaign By Program Id"),
    ]),
    group("Management", "Reference data", "refdata_mgmt", "", "mgmt", [
        G("/pdm/trademarks", "Get Trademarks"), G("/pdm/closures/colors", "Get Closure Colors"),
        G("/pdm/trademarks/:trademarkId/brands", "Get Brands"),
        G("/pdm/trademarks/:trademarkId/brands/:brandId/beverages", "Get Beverages"),
        G("/pdm/:beverageId/primary-containers", "Get Primary Containers"),
        G("/pdm/:beverageId/secondary-packages", "Get Secondary Packages"),
        G("/pdm/closures/diameters", "Get Closure Diameters"),
    ]),
    group("Management", "Organization", "codes_dist", "", "mgmt", [
        G("/orgs", "List All Organizations", cases=[
            neg("Invalid query injection", 200, path="/orgs?sort=DROP TABLE")]),
        G("/orgs/:orgId", "Describe Organization"),
        op("POST", "/orgs", "Create Organization", neg_only=True, cases=[
            neg("Missing OwnerGroup", 400, body={"name": "invalid-org"})]),
    ]),
    # ---------------------------------------------------------------- consumer: realtime and viral
    group("Consumer", "Realtime code", "audit_mgmt", "", "mgmt", [
        G("/pincodes/:realtimeCode.json?programId=:programId", "Realtime Validate", static=True),
        G("/pincodestatus/:realtimeCode.json?programId=:programId", "Realtime Status", static=True),
    ]),
    group("Consumer", "Viral code", RT, "", "realtime", [
        G("/viralcodes/:viralCode.json?programId=:programId", "Viral Validate", static=True, tags=["viral"]),
        op("PUT", "/viralcodes/:viralCode.json?programId=:programId&consumerId=:viralConsumerId"
                  "&transactionId=:viralTransactionId", "Viral Redeem", static=True, tags=["viral", "redeem"]),
        # add the matching unredeem op here, so the same code can be used on the next run
    ]),
    group("Consumer", "Pincode", RT, "", "realtime", [
        G("/pincodes/:realtimeCode.json?programId=:programId", "Pincode Validate", static=True),
        op("PUT", "/pincodes/:realtimeCode.json?programId=:programId&consumerId=:pincodeConsumerId"
                  "&transactionId=:pincodeTransactionId", "Pincode Redeem", static=True, tags=["redeem"]),
        # add the matching unredeem op here
    ]),
    # ---------------------------------------------------------------- consumer: negative cases
    group("Consumer", "Realtime and viral: negative", RT, "", "realtime", [
        op("GET", "", "Validate and status (negative)", neg_only=True, static=True, cases=[
            neg("Realtime Validate Missing ProgramId", 400, path="/pincodes/:realtimeCode.json?programId="),
            neg("Viral Validate Invalid Query Manipulation", 400, path="/viralcodes/:viralCode.json?programId=<script>"),
            neg("Realtime Status Missing Consumer", 400, path="/status.json?programId=:programId&transactionId=:transactionId"),
            neg("Realtime Status Missing TransactionId", 400, path="/status.json?programId=:programId&consumerId=:consumerId"),
            neg("Viral Validate Missing ProgramId", 400, path="/viralcodes/:viralCode.json?programId="),
        ]),
        op("PUT", "", "Redeem (negative)", neg_only=True, static=True, cases=[
            neg("Pincode Redeem Missing Consumer", 400, path="/pincodes/:realtimeCode.json?programId=:programId"),
            neg("Viral Redeem Wrong Program", 400, expect_msg="Invalid program Id",
                path="/viralcodes/:viralCode.json?programId=BADPROGRAM&consumerId=:consumerId&transactionId={trxn}"),
            neg("Pincode Redeem Reused TransactionId", 400,
                path="/pincodes/:redeemedpincode.json?programId=:redeemedprogramId&consumerId=:redeemedconsumerId"
                     "&transactionId=CFTESTRealtimeGenTestCaseA1B2C3"),
            neg("Pincode Redeem Inactive Program", 400,
                path="/pincodes/:inactivepincode.json?programId=:inactivepincodeprogramId"
                     "&consumerId=:redeemedconsumerId&transactionId={trxn}"),
            neg("Redeem Future Viral Code", 400,
                path="/viralcodes/:futureviralCode.json?programId=:futureviralprogramId"
                     "&consumerId=:redeemedconsumerId&transactionId={trxn}"),
            neg("Redeem Already Redeemed Code", 400, expect_msg="Code has already been redeemed", match="exact",
                path="/pincodes/:redeemedpincode.json?programId=:redeemedprogramId"
                     "&consumerId=:redeemedconsumerId&transactionId={trxn}"),
            neg("Blank Code Redeem", 400, expect_msg="Incorrect code for program", match="startswith",
                path="/pincodes/.json?programId=:redeemedprogramId&consumerId=:redeemedconsumerId&transactionId={trxn}"),
            neg("Redeem Expired Viral Code", 400, expect_msg="which expired on", match="contains",
                path="/viralcodes/:inactiveviralCode.json?programId=:inactiveviralprogramId"
                     "&consumerId=:redeemedconsumerId&transactionId={trxn}"),
        ]),
        # not visible in your screenshots: the POST validate call with {"programs": ["BADPROGRAM"], "codes": ["INVALIDCODE"]}
        # (expects 200 and the message "Invalid program Id"). Add it as an op with that body and path.
    ]),
]

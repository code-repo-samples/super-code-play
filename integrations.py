# integrations.py - one entry per integration (type + slot). Imported by config.py.
# Edit freely: put a # in front of any line (or a whole { ... } block) to switch it off.
# `python sftp_load.py scan` appends new entries for new sample folders at the end of the list.
#
# Fields
#   "type"            folder name under project/samples, e.g. "SFTP-HTTP", "SFTP-SFTP", "SFTP-EMAIL"
#   "slot"            the number the sample folder starts with ("12 - ..." -> "12")
#   "upload_path"     SFTP target folder (must start with upload_path_policy.required_prefix) - or None
#   "http_url"        HTTP target URL (use this OR upload_path) - or None
#   "http_form_field" name of the multipart form part ("" = empty name, like curl -F '=@file')
#   "replace_tags"    XML tags whose text gets a new unique value in every file, e.g. ["DocNum"]
#   "copies"          files for this integration when no total / schedule is set (default: defaults["copies"])
#   "max_copies"      upper limit for this integration whatever the total is
#   "weight"          bigger = gets more files than the others in a round robin (default 1)
#   "name_mode"       force the file-name rule: "token" | "tail" | "insert" (default: automatic)
#   "replace_last_n" / "append_suffix" / "tag_value_length"   per-integration overrides of defaults
#   "http_headers"    extra headers for this integration only
#   "label", "integration", "sender", "receiver", "data_type", "sr_no"   descriptive only (shown in reports)

INTEGRATIONS = [
    {
        "sr_no": 1,
        "slot": "1",
        "type": "HTTP-SFTP",
        "label": "CONASystem_ALLOY_XMLIDOCCONA_OUT_DELVRY07_DESADV",
        "integration": "CONA System > XML IDOC CONA_OUT_DELVRY07_DESADV > ALLOY",
        "sender": "CONA System",
        "receiver": "ALLOY",
        "data_type": "XML IDOC CONA_OUT_DELVRY07_DESADV",
        "http_url": "http://http-gateway-qa.example.com/ws/REPLACE_WITH_REAL_URL",
        "http_form_field": "",
        "upload_path": None,
        "replace_tags": [],
        "_comment": "slot = the number the sample folder starts with (1 - ...). Optional per integration: copies, max_copies, weight, name_mode (token|tail|insert), replace_last_n, append_suffix, http_headers, serial.",
    },
    {
        "sr_no": 1,
        "slot": "1",
        "type": "SFTP-EMAIL",
        "label": "CBS_Durango_FFTextApplicationAdvice",
        "integration": "Email delivery for CBS > FF Text ApplicationAdvice > Durango",
        "sender": "CBS",
        "receiver": "Durango",
        "data_type": "FF Text ApplicationAdvice",
        "upload_path": "/spice/qa-final/opt/liaison/fromalloy/topartner/",
        "replace_tags": [],
        "_comment": "replace_tags example: [\"DocNum\", {\"tag\": \"TransactionNumber\", \"prefix\": \"XYZ\", \"length\": 10}]",
    },
]

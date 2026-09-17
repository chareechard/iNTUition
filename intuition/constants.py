# Blackboard and local authentication URLs.
NTULEARN_URL = "https://ntulearn.ntu.edu.sg"

# Current identity provider (Microsoft Entra ID). Interactive login only (MFA enforced).
ENTRA_TENANT_ID = "15ce9348-be2a-462b-8fc0-e1765a9b204a"
ENTRA_SAML_URL = "https://login.microsoftonline.com/{}/saml2".format(ENTRA_TENANT_ID)

# The page to open in a browser to obtain a session cookie manually.
LOGIN_LANDING_URL = NTULEARN_URL + "/ultra"

# NTU Blackboard endpoints (Original course view - legacy fallback)
GET_COURSES_URL = (
    "https://ntulearn.ntu.edu.sg/webapps/blackboard/execute/globalCourseNavMenuSection"
)
GET_CONTENT_IDS_URL = (
    "https://ntulearn.ntu.edu.sg/webapps/blackboard/execute/announcement"
)
GET_CONTENT_LIST_URL = (
    "https://ntulearn.ntu.edu.sg/webapps/blackboard/content/listContent.jsp"
)

# Blackboard Learn public REST API (works for both Ultra and Original courses)
REST_BASE_URL = NTULEARN_URL + "/learn/api/public"
REST_VERSION_URL = REST_BASE_URL + "/v1/system/version"
REST_MY_COURSES_URL = REST_BASE_URL + "/v1/users/me/courses"
REST_ME_URL = REST_BASE_URL + "/v1/users/me"
REST_CALENDAR_ITEMS_URL = REST_BASE_URL + "/v1/calendars/items"

# Ultra's *internal* API. Not part of the documented public contract, but it is the only
# place the Favourites (starred courses) flag is exposed - the public
# /users/me/courses memberships carry no favourite field at all. Verified against NTU's
# instance: ?favorite=true narrows 41 enrolments to the 7 starred ones.
REST_INTERNAL_MEMBERSHIPS_URL = NTULEARN_URL + "/learn/api/v1/users/me/memberships"
REST_COURSE_CONTENTS_URL = REST_BASE_URL + "/v1/courses/{course_id}/contents"
REST_CONTENT_CHILDREN_URL = (
    REST_BASE_URL + "/v1/courses/{course_id}/contents/{content_id}/children"
)
REST_CONTENT_ATTACHMENTS_URL = (
    REST_BASE_URL + "/v1/courses/{course_id}/contents/{content_id}/attachments"
)
REST_ATTACHMENT_DOWNLOAD_URL = (
    REST_BASE_URL
    + "/v1/courses/{course_id}/contents/{content_id}/attachments/{attachment_id}/download"
)

# Blackboard content handler ids we care about when walking the REST content tree
FOLDER_HANDLERS = frozenset(
    ["resource/x-bb-folder", "resource/x-bb-lesson", "resource/x-bb-learningmodule"]
)
DOCUMENT_HANDLERS = frozenset(
    ["resource/x-bb-document", "resource/x-bb-file", "resource/x-bb-assignment"]
)

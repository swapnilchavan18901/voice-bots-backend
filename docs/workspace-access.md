# Workspace access and frontend boundaries

The backend is the authority for identity, workspace membership, resource ownership,
roles, calendar routing and visit eligibility. Client selection state, hidden buttons,
query parameters and cached permission flags never grant access.

`require_membership` is shared by the workspace, bot, team, API key and meeting APIs.
`MANAGE_ROLES` defines owner/admin write access centrally. Each mutation rechecks
current membership; a browser refresh or new login is not required to revoke access.
Nested resource lookups also constrain their workspace or bot ID. Operator status does
not bypass workspace membership or organizer-only calendar assignment.

| Area | Backend scope and checks |
| --- | --- |
| Organizations and workspace choices | Current authenticated user's memberships only |
| Workspace overview | Membership before loading data; bots and members constrained to the requested organization |
| Bots and feature lists | Organization-owned bots; active templates grouped by clinic/conversation on the server |
| Clinic patients and visits | Authorized organization and bot; appointment ID belongs to that bot; owner/admin writes; cancelled and future visits rejected |
| Members and invitations | Membership for reads; owner/admin invitation writes; only owners can invite admins; invitations bound to their email |
| API keys | Owner/admin access; key ID constrained to organization; hashed tokens with expiry and revocation |
| Calendar inbox and choices | Trusted linked organizer identity; each user's own current memberships; no owner/operator visibility override |
| Meetings and shared memory | Organization membership and meeting-to-organization match; operator plus membership for bot controls |
| Public voice | API key organization matches bot; expiring single-use grant bound to key and bot; revocation rechecked |
| Meeting audio callbacks | Signed, expiring capture token; does not accept a caller-selected workspace |

The frontend loads `GET /api/v1/organizations/{org_id}/workspace` for prepared feature
lists and capabilities (`can_manage`, `can_invite_admins`). It no longer derives roles
from the member list or classifies workspace bots itself. It keeps only presentation
state: selected workspace, search text, open dialogs and in-progress requests.

The patient view loads `GET /api/v1/organizations/{org_id}/bots/{bot_id}/clinic/patients`.
An optional `day=YYYY-MM-DD` selects a date; otherwise the server uses today in the
clinic's timezone. The response contains patient rows, counts and `can_update_visit`
for each row. The UI refreshes this response after a mutation instead of recalculating
counts or appointment rules. Legacy appointment list endpoints remain available.

Regression tests cover cross-workspace IDs, substituted parent IDs, role escalation,
revoked membership, organizer privacy, unverified email claims, public voice token
scope and PATCH preflight behavior. Dashboard CORS permits PATCH for configured
origins; the separate public voice CORS policy remains POST/DELETE only.

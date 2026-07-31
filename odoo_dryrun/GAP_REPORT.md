# Odoo free-tier migration — dry run gap report

Generated from real `peershop.db` (9 sellers, 32 products, 11 departments, 29 categories).
Target: **Odoo Online "One App Free"** (eCommerce/Website) — UI CSV import only, **no custom modules**.

## What imports cleanly (stock Odoo, free tier)
- **Categories** → `product.public.category` (dept as parent, category as child). All 11 depts + 29 cats map 1:1.
- **Products** → `product.template` (name, price, description, published flag, one public category). 31/32 carry a category.
- **Seller names/contacts** → `res.partner` contacts (informational only).

## What BREAKS or is LOST on the free tier
| Item | Problem | Severity |
|---|---|---|
| **Multi-vendor ownership** | Stock Odoo has no "this product belongs to seller X's storefront". `seller_id` has no target field. Needs a paid marketplace module (~$300) that the free tier **cannot install**. | 🔴 blocker |
| **Subcategories** | 0 seeded now, but the 3rd taxonomy level has no clean stock home (public categories are 2 usable levels in practice). | 🟡 |
| **Attributes (freeform JSON)** | No clean stock target; Odoo attributes drive variants, not freeform text. Folded into description text. | 🟡 |
| **Status vocabulary** | `pending/live/rejected/removed` → collapses to `is_published` true/false. Governance/approval flow is gone. | 🔴 (loses core model) |
| **AI pipeline** (describe/categorize/chat/insights) | Custom code — banned on free tier. | 🔴 |
| **Telegram bots + /post wizard** | Custom code — banned on free tier. | 🔴 |
| **Pending-approval queues, seller_attribute_history, chat_logs, nudges** | Custom — no home. | 🔴 |
| **Prices** | Many demo products have null→0 price; would import as $0 (data hygiene, not Odoo's fault). | 🟢 note |
| **Product photos** | 3 in `product_photos` + `image_path`; must be re-uploaded/attached by URL during import. | 🟡 |

## Verdict
On the **free hosted tier** you can reproduce a *plain single-vendor catalog* (categories + products + prices + photos). Everything that makes PeerShop distinct — multi-vendor, human-in-the-loop AI governance, Telegram control — **cannot come along**, because it is custom code the free tier forbids. This confirms the free tier is not a viable migration target for the actual project.

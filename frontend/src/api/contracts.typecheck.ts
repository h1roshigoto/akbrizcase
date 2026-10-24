// Compile-time contract checks, included by the existing typecheck/build commands.
import { fixtures } from "./fixtures"
import type {
  CatalogItem,
  ChatMessageCreate,
  Contact,
  ImageURL,
  OrderCreate,
  OrderReceipt,
  OrderStatusChange,
  SecretToken,
  SourceRef,
} from "./types"

// @ts-expect-error money is serialized as a decimal string
export const numericPrice: CatalogItem["price"] = 1200
// @ts-expect-error image links are restricted to the catalog image endpoint
export const externalImage: ImageURL = "https://example.org/image.png"
// @ts-expect-error sources link to an exact document version
export const externalSource: SourceRef["url"] = "https://example.org/file"
// @ts-expect-error an arbitrary string is not a validated token
export const tokenURL: SecretToken = "https://example.org/token"
// @ts-expect-error null price is required when unknown, omission is not equivalent
export const missingPrice: CatalogItem = { ...fixtures.catalog_item, price: undefined }
// @ts-expect-error exactOptionalPropertyTypes rejects explicit undefined
export const undefinedEmail: Contact = { name: "Демо", email: undefined }
export const omittedEmail: Contact = { name: "Демо", phone: "+0 000 000 0000" }
export const nullEmail: Contact = { ...omittedEmail, email: null }
const withoutTotal: Omit<OrderReceipt, "items_total"> = fixtures.receipt
// @ts-expect-error nullable items_total must be present
export const missingTotal: OrderReceipt = withoutTotal
// @ts-expect-error status is an enum
export const arbitraryStatus: OrderStatusChange["status"] = "shipped"
// @ts-expect-error next/node_id are never accepted from clients
export const directTransition: ChatMessageCreate["input"] = { type: "button", option_id: "demo", next: "result" }
// @ts-expect-error chat modes are an enum
export const unknownMode: ChatMessageCreate["input"] = { type: "select_mode", mode: "repair" }
const { comment: ignoredComment, ...supportWithoutComment } = fixtures.support_order
void ignoredComment
// @ts-expect-error support requires an independently confirmed description
export const missingSupportComment: OrderCreate = supportWithoutComment
// @ts-expect-error source=chat requires chat_session_id
export const missingChatSession: OrderCreate = { ...fixtures.support_order, source: "chat" }
// @ts-expect-error source=site forbids chat_session_id, including null
export const siteWithSession: OrderCreate = { ...fixtures.support_order, chat_session_id: null }
// @ts-expect-error callers cannot submit a price with order items
export const clientPrice: OrderCreate["items"][number] = { item_id: fixtures.catalog_item.id, qty: 1, expected_revision: 3, price: "1.00" }

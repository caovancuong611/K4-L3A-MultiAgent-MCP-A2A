# L3A Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của workflow. Hệ thống không ghi
prompt bí mật, chain-of-thought hoặc credential vào output/trace.

## 1. System overview

```text
inputs/<case_id>.json
          │
          ▼
Coordinator ── task_assigned ──┬─ Order/item agent ── MCP order/item/seller
                               ├─ Payment agent ───── MCP payment/refund
                               ├─ Shipment agent ──── MCP shipment
                               └─ Policy agent ────── MCP policy
                                         │
                           evidence + handoff
                                         ▼
                                     Verifier
                                         │
                         schema-safe output + trace
```

Claim của khách hàng chỉ được dùng làm giả thuyết định tuyến. Verifier tự xác minh
issue bằng evidence có thẩm quyền. Mọi record có timestamp được giới hạn trong cửa
sổ `[order_purchase_timestamp, opened_at]`; record trước khi mua hoặc sau khi mở
case không được dùng để kết luận.

Workflow là state machine async thuần Python, không dùng LLM. Điều này tránh phụ
thuộc model/API ngoài và làm kết quả lặp lại được.

## 2. Agent ownership

| Actor | Input | Tool được phép gọi | Trách nhiệm và handoff |
| --- | --- | --- | --- |
| Coordinator | case public | Không gọi data tool | Kiểm tra ID, định tuyến tối thiểu, emit `task_assigned` |
| Order/item agent | `case_id`, `order_id` | `get_order`, `get_order_items`, và `get_sellers` khi cần seller responsibility | Xác lập lifecycle, item/seller trong scope; handoff evidence cho verifier |
| Payment agent | `case_id`, `order_id` | `get_payment_timeline`, `get_refund_timeline` chỉ cho refund cases | Đối soát capture, split, mismatch, duplicate và refund state |
| Shipment agent | `case_id`, `order_id` | `get_shipment_summary` | So sánh shipping limit, carrier handoff, ETA và delivery |
| Policy agent | `case_id`, `policy_version` | `get_policy` | Chọn đúng rule cho issue đã xác minh; emit `policy_decided` |
| Verifier | specialist handoffs | Không gọi tool | Lọc time scope, giải quyết conflict, khóa entity/refund/action và tạo output |

Không agent nào nhận API key. Gateway là thành phần duy nhất thêm authorization
header, validate MCP envelope và trả evidence đã được contract kiểm tra.

## 3. A2A protocol

Logical message envelope gồm `case_id`, `actor`, `target`, `decision_code`,
`tool_name`, `evidence_refs` và scalar attributes. `case_id` là correlation key bắt
buộc cho mọi task, handoff và MCP call.

Coordinator chỉ tạo một assignment cho mỗi specialist cần thiết. Specialist chỉ
handoff một lần tới verifier sau khi hoàn tất danh sách tool hữu hạn; verifier trả
một kết quả về coordinator. Không có cạnh quay về specialist nên không thể tạo vòng
lặp. Các specialist độc lập chạy đồng thời bằng `asyncio.gather`; tool trong cùng
specialist chạy tuần tự để giữ lifecycle dễ audit.

Trace chỉ chứa event quan sát được: assignment, tool consumption, handoff, policy
decision và verification result. Không ghi reasoning riêng.

## 4. Evidence lifecycle

1. Gateway luôn ghép `case_id` từ case hiện tại vào payload và validate
   `mcp-evidence-response-v1`.
2. Workflow giữ nguyên `evidence_ref`; không sửa, hash lại hoặc tự sinh ref.
3. Mỗi response được dùng sẽ emit `tool_result_consumed` ngay với đúng actor/tool.
4. Evidence được giữ trong memory của một lần `solve_case`; không có cache chéo case.
5. Verifier chỉ dùng row/event đúng `order_id` và đúng case-time window; bản ghi
   trùng hệt được khử trùng, còn nhiều row cùng entity/payment reference được đối
   soát theo capture hợp lệ sớm nhất.
6. Với refund case, amount và payment reference được liên kết trực tiếp với event
   trong refund timeline; capture không liên quan được ghi thành conflict thay vì
   cộng vào tổng hoàn tiền.
7. Global `evidence_refs`, claim linkage và verification trace đều lấy từ cùng tập
   response MCP đã validate.

Policy quyết định `case_status`, action và refund. Entity ID thuộc case (đặc biệt
seller) lấy từ order-item/shipment evidence; nếu policy chứa entity khác scope thì
case-scoped source thắng và conflict được ghi rõ.

## 5. Failure policy

| Failure | Retry? | Fallback | Observable result |
| --- | --- | --- | --- |
| MCP transport/server error | Tối đa 3 lần reconnect có backoff, session mới sau mỗi 5 case | Resume từ case chưa commit | Output và trace của một case chỉ commit cùng nhau; không tạo artifact dở |
| Tool trả not-found/mismatched order | Không | Dừng case/run | Validation error trước finalize |
| MCP envelope/schema sai | Không | Dừng case/run | Contract error từ gateway |
| Record ngoài time scope | Không cần | Loại record | `data_conflicts` / `EXCLUDE_OUTSIDE_CASE_WINDOW` |
| Policy entity xung đột case | Không cần | Chọn case-scoped entity | `CASE_SCOPED_ENTITY_WINS` |
| Specialist thiếu evidence bắt buộc | Không | Dừng case/run | Không phát `verification_completed` |

Workflow không biến missing evidence thành `evidence_ref`, amount hoặc entity phỏng
đoán. `day09 run --resume` chỉ bỏ qua case đã có output hợp lệ và event
`case_finalized`; case lỗi được chạy lại từ đầu với session mới.

## 6. Verification invariants

Trước khi trả output, verifier đảm bảo:

- `case_id`/`order_id` của input, MCP payload và MCP data cùng scope;
- mọi timestamp dùng để kết luận nằm giữa purchase và `opened_at`;
- evidence refs là duy nhất, đến trực tiếp từ các response của case hiện tại và đã
  có event `tool_result_consumed`;
- item/seller/payment reference chỉ được lấy từ record trong scope;
- primary issue được evidence xác minh, không sao chép mù claim topic;
- status, action, refund và responsible party phù hợp rule policy đã chọn;
- tổng `refund_lines` bằng `recommended_refund_brl`, hoặc cả hai bằng 0;
- confidence nằm trong `[0, 1]` và giảm nếu evidence bác bỏ giả thuyết ban đầu;
- output cuối pass `l3a-output-v2.schema.json` trong CLI trước khi ghi file.

## 7. Reproducibility

- Runtime: Python 3.11+, dependency ranges khóa trong `pyproject.toml`.
- Decision engine: deterministic Python rules; không dùng model, temperature hoặc
  random seed.
- Concurrency: tối đa số specialist của một case (4); CLI xử lý case tuần tự và
  làm mới MCP session theo batch 5 case.
- Tiền tệ: `Decimal`, chỉ chuyển sang số JSON sau khi quantize 2 chữ số.
- Config/secret: chỉ đọc từ `.env`, file này bị Git ignore và không đóng gói.
- Lệnh chuẩn:

```bash
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

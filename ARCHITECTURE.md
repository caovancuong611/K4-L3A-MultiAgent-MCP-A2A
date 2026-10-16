# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý một case (`day09 run` → [cli.py](src/student_agent/cli.py) → `solve_case` trong [workflow.py](src/student_agent/workflow.py)):

```text
inputs/<case_id>.json
      │  (case_received)
      ▼
 Coordinator ──task_assigned──► Order agent ──handoff──► Payment agent ──handoff──► Shipment agent ──handoff──► Policy agent ──handoff──► Verifier
                                   │                        │                         │                         │                         │
                                   ▼                        ▼                         ▼                         ▼                         ▼
                      CaseEvidenceCollector (case_id cố định, cache, emit tool_result_consumed)             investigation.py (thuần, offline)
                                   │                                                                                                       │
                                   ▼                                                                                                       ▼
                      EvidenceGateway.call() ── MCP streamable HTTP (Bearer sk-team-…) ──► MCP Evidence Gateway        outputs/<case_id>.json
                                                                                                                       + verification_completed
                                                                                                                       + case_finalized
                                         traces/trace.jsonl  ◄── mọi sự kiện quan sát được
```

- Các case chạy **tuần tự** theo thứ tự trong `case-set.json`; một MCP session dùng chung cho toàn bộ run.
- Tầng suy luận nghiệp vụ ([investigation.py](src/student_agent/investigation.py)) là hàm thuần trên `data` đã fetch, không gọi mạng → unit test được bằng fixture (`tests/test_investigation.py`).
- Output được validate theo `l3a-output-v2.schema.json` và kiểm tra `case_id` trước khi ghi (ghi file tạm rồi `replace` để atomic).

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `case_id`, `customer_request` (claimed_order_id, claims), `policy_version` | Nhận case, giao việc (`task_assigned`) cho từng specialist theo thứ tự cố định, không tự gọi tool | `case_received`, `task_assigned` ×4, `case_finalized` |
| Order/item (`order-agent`) | `claimed_order_id` | Lấy order + items, chọn dòng item chuẩn (`pick_canonical_items`) khi có dòng trùng mâu thuẫn | `OrderFacts`, canonical items, item conflicts → `handoff` sang payment-agent |
| Payment (`payment-agent`) | `order_id`, items chuẩn, `order_approved_at` | Tổng tiền đã trả; phân cụm payment timeline để phát hiện `payment_mismatch` / `duplicate_charge` / `valid_split_payment`; đánh giá refund timeline (`refund_pending` / `refund_failed`), loại sự kiện decoy theo thời gian và số tiền | `PaymentFinding`, refund issue → `handoff` sang shipment-agent |
| Shipment (`shipment-agent`) | `order_id`, order timestamps, items chuẩn | So `delivered_customer_date` với ước tính; xác định trễ do seller (giao carrier sau `shipping_limit_date`) hay logistics; đối chiếu sự kiện `delivered_late` (xác nhận hoặc mâu thuẫn) | `DeliveryFinding` → `handoff` sang policy-agent |
| Policy (`policy-agent`) | `policy_version` | Lấy bảng rule theo version; không suy diễn lại tiền/trạng thái | `PolicyFacts` → `handoff` sang verifier |
| Verifier | Tất cả finding + evidence record | Chọn `primary_issue` theo thứ tự ưu tiên, áp rule policy, ghép claim ↔ evidence, kiểm tra consistency, gán confidence | Output JSON + `verification_completed` (decision_code = primary_issue) |

**Quyền gọi tool (least privilege):**

| Actor | Tool được gọi |
| --- | --- |
| order-agent | `get_order`, `get_order_items` |
| payment-agent | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |
| coordinator, verifier | không gọi tool — chỉ dùng evidence đã được specialist fetch |

Tên tool luôn được kiểm tra với danh sách `list_tools()` (discovery) trước khi gọi.

## 3. A2A protocol

- **Envelope:** mỗi message giữa agent được biểu diễn bằng một trace event `day09-trace-event-v1` (`event_id`, `case_id`, `event_type`, `occurred_at`, `actor`, `target`, tùy chọn `decision_code`, `tool_name`, `evidence_refs`, `attributes`). Payload nội bộ giữa agent là dataclass bất biến (`EvidenceRecord`, `OrderFacts`, `PaymentFinding`, `DeliveryFinding`, `PolicyFacts`).
- **Correlation:** mọi event và mọi MCP call mang cùng `case_id`; `CaseEvidenceCollector` được khởi tạo với một `case_id` duy nhất nên specialist không thể truy vấn chéo case.
- **Handoff:** pipeline tuyến tính `order → payment → shipment → policy → verifier`. Handoff xảy ra khi specialist hiện tại xong (thành công hoặc degrade). Lỗi ở evidence bắt buộc (order/items, payments, policy) → handoff thẳng sang verifier với `attributes.reason` và kết luận `insufficient_evidence`.
- **Timeout:** HTTP connect/write/pool 30 s, read 300 s (cho phép gateway xử lý chậm); mỗi tool call retry tối đa 1 lần (xem §5).
- **Tránh vòng lặp:** luồng là DAG cố định, không có handoff ngược; mỗi (tool, arguments) chỉ gọi một lần nhờ cache; số MCP call mỗi case bị chặn trên bởi 7.
- Trace chỉ chứa sự kiện và decision code quan sát được, không chứa prompt hay nội dung suy luận.

## 4. Evidence lifecycle

Toàn bộ MCP call đi qua `EvidenceGateway.call()` ([mcp_gateway.py](src/student_agent/mcp_gateway.py)):

1. **Discovery, không đoán tên tool.** `list_tools()` cache danh sách tool thật từ gateway; `call()` chặn ngay ở phía client nếu `tool_name` không nằm trong danh sách đã discover, trước khi gửi request (audited) lên server.
2. **Validate schema.** `Contracts.validate_evidence()` kiểm tra mọi response theo `mcp-evidence-response-v1.schema.json` trước khi trả về cho caller — response sai contract sẽ raise ngay, không lọt xuống specialist.
3. **Lưu evidence_ref nguyên trạng.** `CaseEvidenceCollector` ([evidence.py](src/student_agent/evidence.py)) bọc gateway cho một `case_id` cố định (truyền vào constructor), chỉ đọc `evidence_ref`/`data`/`domain` từ response — không sửa, không tự sinh. Vì `case_id` được collector tự truyền cho mọi call, một specialist dùng collector không thể vô tình lấy evidence chéo case.
4. **Cache theo (tool, arguments).** Gọi lại cùng tool + cùng tham số trong một case sẽ trả từ cache thay vì gọi lại gateway (giảm audited call trùng lặp).
5. **Emit `tool_result_consumed` tại điểm fetch.** Mỗi lần fetch evidence mới, collector tự `trace.emit(...)` với `actor` là specialist gọi nó và `evidence_refs=[evidence_ref]`, đảm bảo mọi evidence đã "consume" đều có dấu vết observable.
6. **Map evidence → claim/output ở verifier.** Fetch được evidence không đồng nghĩa được trích dẫn: verifier chỉ đưa vào `evidence_refs` các record hỗ trợ trực tiếp `primary_issue` đã chọn, cộng với policy record:

   | primary_issue | Evidence trích dẫn |
   | --- | --- |
   | `canceled_order_paid`, `unavailable_order_paid` | order, payments, policy |
   | `payment_mismatch`, `duplicate_charge`, `valid_split_payment` | payments, payment timeline, policy |
   | `refund_pending`, `refund_failed` | refund timeline, policy |
   | `late_delivery_*` | order, items, shipment summary, policy |
   | `unsupported_claim` | order, payments, policy |
   | `insufficient_evidence` | các record đã fetch được trước khi lỗi |

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / mất kết nối (`httpx2.TransportError`, `TimeoutError`, `MCPError` như 502) | Có, 1 lần sau 0,5 s (call chỉ đọc nên idempotent) | Evidence bắt buộc → `insufficient_evidence`; evidence phụ (payment/refund timeline, shipment) → bỏ qua tín hiệu đó, tiếp tục với evidence còn lại | `handoff` → verifier với `attributes.reason`; `verification_completed` decision_code `insufficient_evidence` |
| Not found (tool trả `is_error`) | Không (lỗi nghiệp vụ, retry không đổi kết quả) | Như trên: bắt buộc → `insufficient_evidence`, phụ → bỏ qua | như trên |
| Source conflict (dòng item/payment trùng mâu thuẫn, sự kiện decoy) | Không | Chọn nguồn chuẩn theo quy tắc xác định: cụm gần `order_approved_at` nhất; refund event trước ngày duyệt hoặc sai số tiền bị loại; `delivered_late` mâu thuẫn timestamp bị loại | Ghi vào `data_conflicts` với `selected_source` và `resolution_code` (`picked_row_closest_to_order_approval`, `event_inconsistent_with_authoritative_timestamps`) |
| Invalid specialist result / evidence sai schema (`ValueError`, `KeyError`) | Không | Coi như lookup thất bại → degrade, không bịa dữ liệu | như dòng timeout |
| Output không qua schema / sai `case_id` | Không | Dừng run (fail fast) để không nộp output hỏng | — |
| Gateway không reachable khi bắt đầu run | Không | Run dừng **trước khi** xóa outputs/trace của lần chạy trước | — |

Retry có giới hạn và idempotent. Missing evidence không bao giờ được chuyển thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước khi finalize một case:

- **Schema:** output validate theo `l3a-output-v2.schema.json`; mỗi trace event validate theo `trace-event-v1.schema.json` ngay lúc emit; `day09 validate` kiểm tra lại toàn bộ outputs khớp `case-set.json`.
- **Entity scope:** `order_ids`, `payment_references`, `shipment_ids` chỉ chứa `claimed_order_id` của case; `item_ids`/`seller_ids` lấy từ các dòng item chuẩn.
- **Evidence ownership:** mọi `evidence_ref` đến từ response MCP của chính case đó (collector cố định `case_id`), không sửa, không trùng lặp (`_dedup_refs`).
- **Claim linkage:** mỗi claim trong input có đúng một `claim_assessment`; verdict `supported` chỉ khi topic trùng `primary_issue` (hoặc `requested_full_refund` khi `case_status = action_required`); evidence_refs của claim là tập trích dẫn của kết luận.
- **Money totals:** so sánh tiền với dung sai 0,01 BRL; `recommended_refund_brl` lấy từ rule policy; `refund_lines` có tổng bằng `recommended_refund_brl` và rỗng khi refund = 0.
- **Responsibility/action consistency:** `case_status`, `resolution_actions`, `responsible_parties.party_type` cùng lấy từ một rule policy của `primary_issue`; `party_id` chỉ gán seller khi `party_type = seller` (late delivery); `ranked_causes[0].cause_code` = `primary_issue` viết hoa.
- **Confidence bounds:** confidence trong [0, 1], hiệu chỉnh theo độ trực tiếp của tín hiệu (0,95 cho order status rõ ràng … 0,6 cho `unsupported_claim`, 0,2 cho `insufficient_evidence`; late delivery 0,9 nếu có shipment event xác nhận, 0,75 nếu chỉ có timestamp).

## 7. Reproducibility

- **Model/config:** không dùng LLM — toàn bộ quyết định là rule xác định trên evidence, nên cùng evidence cho cùng output. Không có random seed (chỉ `event_id` trong trace là ngẫu nhiên).
- **Runtime đã kiểm thử:** Python 3.14.7; `mcp` 2.2.0, `httpx2` 2.13.1, `jsonschema` 4.26.0, `python-dotenv` 1.2.3 (khoảng phiên bản được pin trong `pyproject.toml`).
- **Concurrency:** 1 — các case và các tool call chạy tuần tự trên một MCP session.
- **Giới hạn tài nguyên:** tối đa 7 MCP call/case (+1 retry mỗi call), timeout read 300 s, connect 30 s.
- **Cấu hình:** `.env` gồm `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT` (không commit, không đưa vào ZIP).
- **Lệnh chạy:**

  ```bash
  python -m pip install -e ".[dev]"
  pytest -q
  day09 validate-inputs
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```

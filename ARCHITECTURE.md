# L3A Architecture Record

Hồ sơ thiết kế và kiến trúc hệ thống multi-agent điều tra khiếu nại thương mại điện tử Day09 L3A.

## 1. System overview

Luồng điều phối tuần tự và kiểm soát sự kiện từ `inputs/<case_id>.json` qua các Specialist Agents, tương tác MCP Evidence Gateway, qua Verifier tới Output JSON và Trace JSONL:

```text
Input (case.json)
       │
       ▼
 Coordinator Agent ────────────────────────────────────────┐
       │                                                   │
       ├─► [task_assigned] ──► Order Specialist ──► MCP ───┤
       │                            │ (evidence)           │
       │                            ▼                      │
       │                       [handoff]                   │
       │                                                   │
       ├─► [task_assigned] ──► Payment Specialist ► MCP ───┤
       │                            │ (evidence)           │
       │                            ▼                      │
       │                       [handoff]                   │
       │                                                   │
       ├─► [task_assigned] ──► Shipment Specialist► MCP ───┼──► TraceWriter
       │                            │ (evidence)           │    (trace.jsonl)
       │                            ▼                      │
       │                       [handoff]                   │
       │                                                   │
       ├─► [task_assigned] ──► Policy Specialist ─► MCP ───┤
       │                            │ (evidence, policy)   │
       │                            ▼                      │
       │                    [policy_decided]               │
       │                       [handoff]                   │
       ▼                                                   │
 Verifier Agent ◄──────────────────────────────────────────┘
       │
       ▼
 [verification_completed]
       │
       ▼
 Output (outputs/<case_id>.json) + [case_finalized]
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | MCP Tools được cấp phép | Output / Handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | `case` dict (`case_id`, `customer_request`, `policy_version`) | Điều phối toàn bộ vòng đời case, giao nhiệm vụ điều tra, thu thập facts, chẩn đoán `primary_issue` | *Không gọi trực tiếp MCP* | `task_assigned`, handoff tới các Specialist & Verifier |
| `order-agent` | `case_id`, `claimed_order_id` | Xác minh thông tin đơn hàng, danh sách item, seller, giá bán, cước vận chuyển | `get_order`, `get_order_items`, `get_sellers` | `handoff` (decision_code: `order_findings_submitted`) |
| `payment-agent` | `case_id`, `claimed_order_id` | Phân tích phương thức thanh toán, số đợt trả góp, phát hiện duplicate charge, đối soát số tiền và tiến trình hoàn tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `handoff` (decision_code: `payment_findings_submitted`) |
| `shipment-agent` | `case_id`, `claimed_order_id` | Xác minh mốc thời gian giao hàng thực tế vs dự kiến, hạn giao hàng của seller, phát hiện sự kiện giao trễ và quy trách nhiệm (`seller` vs `logistics_provider`) | `get_shipment_summary` | `handoff` (decision_code: `shipment_findings_submitted`) |
| `policy-agent` | `case_id`, `policy_version`, `primary_issue` | Tra cứu điều khoản chính sách chính thức, xác định trạng thái vụ việc (`case_status`), số tiền hoàn (`refund_brl`), hành động khuyến nghị (`recommended_action`) | `get_policy` | `policy_decided`, `handoff` (decision_code: `policy_findings_submitted`) |
| `verifier` | Raw case output dict | Kiểm tra các bất biến nghiệp vụ, schema `day09-l3a-output-v2`, kiểm tra tính nhất quán chéo (status vs refund vs action, seller responsibility ID) | *Không gọi trực tiếp MCP* | `verification_completed` (decision_code: `verification_passed`) |

## 3. A2A protocol

- **Message Envelope & Trace Correlation**: Mọi sự kiện giao tiếp và chuyển giao giữa các agent được lưu vết có cấu trúc thông qua `TraceWriter`, tương thích chuẩn `day09-trace-event-v1`. Mỗi event gắn với `case_id`, `occurred_at`, `actor`, `target`, `decision_code`, và `evidence_refs`.
- **Luồng Acyclic**: Luồng đi theo đồ thị có hướng không chu trình: `Coordinator` -> Specialists -> `Coordinator` -> `Verifier`. Tuyệt đối không tạo vòng lặp vô hạn.
- **Quan sát được (Observable)**: Chỉ trace các sự kiện vòng đời và mã quyết định quan sát được (`case_received`, `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`, `case_finalized`), không lưu prompt nội bộ hay chain-of-thought.

## 4. Evidence lifecycle

- **Thu thập có thẩm quyền**: Mọi dữ liệu nghiệp vụ phải được truy xuất qua `EvidenceGateway.call()` với đúng `case_id`.
- **Validation**: Mỗi phản hồi MCP được kiểm tra hợp lệ theo JSON Schema `mcp-evidence-response-v1.schema.json`.
- **Lưu vết Provenance**: Ngay khi một specialist agent tiêu thụ dữ liệu từ MCP, emit sự kiện `tool_result_consumed` với `evidence_refs=[evidence["evidence_ref"]]`.
- **Cô lập Scope**: `evidence_ref` chỉ được dùng cho case hiện tại, không chia sẻ hay dùng chéo case.

## 5. Failure policy

| Tình huống lỗi | Retry? | Fallback | Trace event / Decision Code |
| --- | --- | --- | --- |
| MCP timeout | Tối đa 2 lần với backoff ngắn | Báo lỗi hoặc ghi nhận `insufficient_evidence` | `task_assigned` / `timeout_encountered` |
| Tool 404 / Not found (ví dụ: `get_refund_timeline` khi đơn chưa có hoàn tiền) | Không retry (idempotent) | Trả về cấu trúc rỗng `None` an toàn | Tiếp tục phân tích các bằng chứng khác |
| Xung đột nguồn dữ liệu (Source conflict) | Không | Ưu tiên dữ liệu từ MCP gateway thay vì lời khai khách hàng | Ghi nhận nếu có vào `data_conflicts` |
| Invalid specialist result | 1 lần | Verifier điều chỉnh về bất biến mặc định (`no_action`, 0 refund) | `verification_completed` / `invariant_enforced` |

## 6. Verification invariants

Trước khi hoàn tất case, `VerifierAgent` đảm bảo các bất biến:
1. **Schema compliance**: Đầu ra tuân thủ đầy đủ `day09-l3a-output-v2.schema.json`.
2. **Case status vs Refund consistency**: Nếu `case_status == "no_action"`, `recommended_refund_brl` bắt buộc là `0.0`, `refund_lines` rỗng, và không chứa hành động `issue_refund`.
3. **Seller responsibility ID**: Nếu `party_type == "seller"`, `party_id` phải khớp với `seller_id` thực tế từ đơn hàng.
4. **Unique Actions**: Mảng `resolution_actions` không chứa phần tử trùng lặp.
5. **Confidence Bounds**: Giá trị `confidence` nằm trong khoảng `[0.0, 1.0]`.
6. **Evidence Provenance**: Mọi `evidence_refs` đưa vào output phải nằm trong danh sách đã audit từ các cuộc gọi MCP.

## 7. Reproducibility

- **Môi trường**: Python >= 3.11 (tested trên Python 3.14.3 64-bit Windows).
- **Dependencies**: `httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema[format]>=4.25,<5`, `python-dotenv>=1.1,<2`.
- **Dev dependencies**: `pytest>=8.4,<9`, `ruff>=0.12,<1`.
- **Lệnh thực thi**:
  - Kiểm tra cú pháp & lint: `python -m ruff check src tests manual_mcp_test.py`
  - Unit tests: `python -m pytest -q tests\test_starter.py tests\test_mcp_gateway.py`
  - Kiểm tra bộ input: `day09 validate-inputs`
  - Chạy toàn bộ workflow: `day09 run`
  - Kiểm tra artifacts & trace: `day09 validate`
  - Đóng gói submission: `day09 package --output dist/submission.zip`

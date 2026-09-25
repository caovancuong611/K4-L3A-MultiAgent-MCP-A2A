# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | TODO | TODO | TODO |
| Order/item | TODO | TODO | TODO |
| Payment | TODO | TODO | TODO |
| Shipment | TODO | TODO | TODO |
| Policy | TODO | TODO | TODO |
| Verifier | TODO | TODO | TODO |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Mô tả message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Chỉ trace sự kiện/decision code quan sát được; không trace nội dung suy luận riêng.

## 4. Evidence lifecycle

Toàn bộ MCP call đi qua `EvidenceGateway.call()` ([mcp_gateway.py](src/student_agent/mcp_gateway.py)):

1. **Discovery, không đoán tên tool.** `list_tools()` cache danh sách tool thật từ gateway; `call()` chặn ngay ở phía client nếu `tool_name` không nằm trong danh sách đã discover, trước khi gửi request (audited) lên server.
2. **Validate schema.** `Contracts.validate_evidence()` kiểm tra mọi response theo `mcp-evidence-response-v1.schema.json` trước khi trả về cho caller — response sai contract sẽ raise ngay, không lọt xuống specialist.
3. **Lưu evidence_ref nguyên trạng.** `CaseEvidenceCollector` ([evidence.py](src/student_agent/evidence.py)) bọc gateway cho một `case_id` cố định (truyền vào constructor), chỉ đọc `evidence_ref`/`data`/`domain` từ response — không sửa, không tự sinh. Vì `case_id` được collector tự truyền cho mọi call, một specialist dùng collector không thể vô tình lấy evidence chéo case.
4. **Cache theo (tool, arguments).** Gọi lại cùng tool + cùng tham số trong một case sẽ trả từ cache thay vì gọi lại gateway (giảm audited call trùng lặp).
5. **Emit `tool_result_consumed` tại điểm fetch.** Mỗi lần fetch evidence mới, collector tự `trace.emit(...)` với `actor` là specialist gọi nó và `evidence_refs=[evidence_ref]`, đảm bảo mọi evidence đã "consume" đều có dấu vết observable.
6. **Map evidence → claim/output là việc của tầng verifier (mục 5, chưa triển khai).** Fetch được evidence không đồng nghĩa được trích dẫn: `evidence_refs` trong `claim_assessments`/output cuối chỉ được đưa vào khi evidence đó thật sự hỗ trợ kết luận cụ thể đang đưa ra, không phải mọi record đã fetch trong quá trình điều tra.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Not found | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, evidence ownership, claim linkage, money totals, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và các giới hạn tài nguyên. Không ghi API key.

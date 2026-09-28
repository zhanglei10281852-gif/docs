# DOCX/Markdown 导入幂等化 实现计划

## 一、仓库调研结论

### 后端（Django + DRF + PostgreSQL + S3/MinIO）

两个创建入口，均接受 `file` 多部分上传（DOCX/MD → 外部转换服务 → YJS base64）：

1. **根文档入口**：`POST /api/v1.0/documents/`
   - `DocumentViewSet` 使用 `CreateModelMixin.create` → `perform_create()`（[viewsets.py#L737-L757](file:///f:/swe/092211/project-03/src/backend/core/api/viewsets.py#L737-L757)）
   - `_apply_uploaded_file_conversion()` 先执行昂贵的外部 HTTP 转换（[viewsets.py#L697-L735](file:///f:/swe/092211/project-03/src/backend/core/api/viewsets.py#L697-L735)），随后 `Document.add_root()`（`Document.save` 会先写 DB 再把内容写 S3），最后**独立地** `DocumentAccess.objects.create(OWNER)`。
   - 整个过程**没有事务包裹**：add_root 与 owner 关系之间崩溃会产生"没有所有者的半成品"。

2. **子文档入口**：`POST /api/v1.0/documents/{id}/children/`（[viewsets.py#L1089-L1127](file:///f:/swe/092211/project-03/src/backend/core/api/viewsets.py#L1089-L1127)）
   - 同样先转换，再 `document.add_child()`；**不创建 DocumentAccess**（子文档通过树路径继承权限，测试明确断言 `child.accesses.exists() is False`）。

3. **已有的可利用基础**：
   - `DocumentSerializer.validate_id` 已允许客户端在 POST 时强制指定 UUID（接受 v1/3/4/5），但文档已存在时一律 400（[serializers.py#L237-L264](file:///f:/swe/092211/project-03/src/backend/core/api/serializers.py#L237-L264)）。
   - 文档内容以 `{document_id}/file` 为 key 存于 S3（[models.py#L1053-L1065](file:///f:/swe/092211/project-03/src/backend/core/models.py#L1053-L1065)），因此**身份 = 文档 UUID** 时 S3 key 也是确定性的，崩溃重试会覆盖同一孤儿对象。
   - 路径碰撞已有 `create_tree_node_with_retry()`（内部为 savepoint 原子块，[treebeard.py](file:///f:/swe/092211/project-03/src/backend/core/utils/treebeard.py)）。
   - PostgreSQL 支持会话级 advisory lock，可在不持有长事务的情况下串行化同一身份的并发请求，连接断开时锁自动释放。
   - 最新迁移为 `0033`。

4. **前端（Next.js + React Query）**：
   - 根导入：[useImportDoc.tsx](file:///f:/swe/092211/project-03/src/frontend/apps/impress/src/features/docs/doc-management/api/useImportDoc.tsx) 用 FormData POST `documents/`，成功后把文档**直接前插**到两个列表缓存首页（不过滤重复 id），并弹成功 toast。
   - 拖拽/选择：[useImport.tsx](file:///f:/swe/092211/project-03/src/frontend/apps/impress/src/features/docs/doc-management/hooks/useImport.tsx)；当前文件导入 UI 仅在根级（文档内隐藏），但 API 层将支持 `parentId` 以服务子文档入口。
   - 已依赖 `uuid@14`（含 `v5`）；`crypto.subtle.digest` 可用于文件 SHA-256。
   - `fetchAPI` 支持自定义 headers，但 multipart 下追加表单字段最直接。

### 设计决策

- **可重放身份 = 客户端为"每次文件导入"确定性生成的文档 UUID（v5）**，随 `id` 表单字段提交（根与子文档同一机制）。派生输入：`SHA-256(文件字节) + 文件名 + 目标父级（根为空串）`。同一文件重试（断网/超时后重新选择同一文件、快速双击）身份自动相同；不同文件/父级身份不同。
- **服务端新增 `DocumentImport` 身份记录表**（身份、用户、父级、文件名、文件哈希、content_type、状态 PROCESSING/COMPLETED、文档 FK）。指纹行在转换前**独立短事务先行提交**：转换失败/创建回滚后记录仍在，同文件可安全重试，不同文件凭记录判定 409。
- **并发**：PostgreSQL 会话级 advisory lock（由 UUID 派生 64bit key）。并发同身份请求：一个执行，另一个阻塞等待后重放；持锁者崩溃 → 连接关闭自动放锁 → 后来者接管恢复。
- **崩溃恢复（从确定状态继续）**：转换产物在 DB 创建前写入确定性 S3 key `{id}/file`。接管时探测：文档已存在→补建 owner 关系并置 COMPLETED；文档不存在但 S3 内容已在→跳过昂贵转换直接建文档；都不存在→重新转换。文档行 + root owner 关系 + COMPLETED 标记在**同一个事务**提交，杜绝无主半成品。
- **向后兼容**：不带 `id` 的文件导入走旧路径（服务端生成 UUID，行为同今天）；不带文件的创建完全不变（现有 force_id 400 测试保持有效）。

## 二、文件与模块

### 后端

- `src/backend/core/models.py`：新增 `DocumentImport` 模型（+ `ImportStatusChoices`）。
- `src/backend/core/migrations/0034_documentimport.py`：新增迁移。
- `src/backend/core/services/document_import_service.py`（新文件）：
  - `ImportConflictError`（DRF APIException，HTTP 409）
  - `compute_file_fingerprint(filename, content_type, file_bytes)`
  - `advisory_lock_lock`/释放工具（`pg_advisory_lock(bigint)`，UUID 异或折叠为有符号 64bit）
  - `import_document(*, creator, parent, uploaded_file, document_id, serialized_data) -> (Document, replayed)` 主编排。
- `src/backend/core/api/serializers.py`：`validate_id` 在请求携带文件时跳过"id 已存在"硬拒绝（改由幂等服务判定重放/冲突）；无文件时行为不变。
- `src/backend/core/api/viewsets.py`：
  - `DocumentViewSet.create()`：文件 + id + 转换启用时走幂等服务（201 首次 / 200 重放），否则走现有 `perform_create`。
  - `DocumentViewSet.children()` POST 分支：同样分流；保留权限继承（不建 access）与既有 posthog 属性。
- `src/backend/core/admin.py`：注册 `DocumentImport`（只读友好，便于运维核查）。
- 新增测试 `src/backend/core/tests/documents/test_api_documents_import_idempotency.py`。

### 前端

- 新文件 `src/features/docs/doc-management/utils/importId.ts`：`getImportDocumentId(file, parentId?)`（SHA-256 → uuid v5，WeakMap 按 File 缓存）。
- `api/useImportDoc.tsx`：FormData 追加 `id`；签名支持可选 `parentId`；`onSuccess` 缓存前插按 id 去重（已存在则保持原缓存不动），成功/失败 toast 行为不变。
- `hooks/useImport.tsx`：透传可选 `parentId`（当前调用点均为根级，不传）。
- 新测试 `utils/__tests__/importId.test.ts`：同文件同父级身份稳定、父级不同身份不同、同 File 对象两次调用一致。

## 三、实现步骤（依赖顺序）

1. **模型与迁移**：新增 `DocumentImport`（id=客户端 UUID 主键、creator、parent FK、document FK、filename、file_hash(sha256,64)、content_type、status(PROCESSING/COMPLETED)、created_at/updated_at），`makemigrations` 生成 0034。
2. **幂等服务** `document_import_service.py`：
   - 指纹计算；advisory lock 上下文（try/finally 释放）。
   - 短事务 A（指纹持久化）：`select_for_update` 查记录 → 无则插入 PROCESSING；并发插入撞唯一键则重查。已有记录：校验 creator/parent/filename/hash/content_type 一致，不一致抛 `ImportConflictError(409)`；COMPLETED 且 document 存在 → 直接重放。
   - advisory lock 串行：获锁后二次读状态（双重检查）。
   - 恢复判定：`Document.objects.filter(id=...).first()`；S3 head `{id}/file` 是否存在。
   - 必要时调用 `Converter().convert(...)`（保留 `ConversionError` 上抛，由视图转 400），转换产物经 `default_storage` 写确定性 key。
   - 短事务 B（原子落地）：`create_tree_node_with_retry(add_root/add_child)`；根入口 `get_or_create` OWNER access；记录置 COMPLETED 并关联 document。
   - 首次创建发 `DOC_IMPORTED` + `DOC_CREATED`（子文档带 `document_parent` 属性）；纯重放/修复不重复发事件。
3. **序列化器**：`validate_id` 检测 `self.initial_data.get("file")` 为真实文件时跳过 exists 拒绝。
4. **视图分流**：新增 `create()`；重构 `children()` POST；保持错误码、headers、analytics 旧行为；转换错误仍返回 `{"file": ["Could not convert file content"]}` 400。
5. **前端身份工具 + 接入**：useImportDoc 追加 id、可选 parentId、列表按 id 去重前插；useImport 透传。
6. **测试与验证**（见下）。

## 四、关键约束与注意事项

- 子文档**不得**新建 DocumentAccess（权限继承），与现状一致。
- 标题仍取文件名（文件名优先于显式 title）；文件大小、扩展名校验仍由 `validate_file` 负责；`CONVERSION_UPLOAD_ENABLED=False` 仍 400。
- S3 写入不在 DB 事务内：回滚后确定性 key 上的孤儿对象会被同 id 重试覆盖，不产生多份内容。
- advisory lock 仅做串行化，不承载状态；状态一律以 DB 记录 + S3 探测为准（崩溃后锁自动释放）。
- 重放返回 **200** + 原始文档序列化体；首次创建仍 **201**（前端只判 `response.ok`，无需区分）。
- 旧客户端（无 id）行为与今天完全一致，包括 force-id 已存在的 400。

## 五、验证

- 后端新增测试（`test_api_documents_import_idempotency.py`）：
  1. 根：同 id + 同文件重试 → 200、仅 1 个文档、Converter 仅 1 次、owner access 仅 1 条；
  2. 并发（ThreadPool + Event 卡住 mock 转换）同 id 同文件 → 仅 1 文档、转换仅 1 次；
  3. 同 id 不同文件内容 / 不同文件名 → 409；同 id 根 vs 不同父级 → 409；
  4. 转换失败后同文件重试 → 成功（记录 PROCESSING 可复用）；
  5. 模拟"转换完成 + S3 已有内容 + 文档未建 + PROCESSING"崩溃 → 重试不调用转换、成功建文档；
  6. 模拟"文档行存在但无 owner + PROCESSING" → 重试补 owner、重放文档；
  7. COMPLETED 后在响应丢失场景重试 → 200 返回原文档、不重建；
  8. 子文档：同 id 同文件重放 → 1 个子文档、无 access 行（继承不变）；
  9. 不带 id 的旧路径与无文件创建行为不变。
- 回归：`pytest src/backend/core/tests/documents/` 全量 + 转换服务相关测试。
- 前端：vitest 跑新工具测试；`tsc`/eslint；手工验证（如环境可用）：重复拖拽同一文件列表只出现一次、成功 toast、标题保留扩展名、大小/扩展限制提示不变。

## 六、风险

- **等锁占用 worker**：同身份并发请求 B 会在 advisory lock 上阻塞至 A 完成（转换超时上限 30s 量级），与今天转换本身占 worker 的开销同级；仅影响同用户同身份，互不相关请求不受影响。
- **S3 不可用/无 MinIO 的测试环境**：恢复探测 head_object 异常时按"内容缺失"处理（安全降级为重转），与现有 `content` getter 的容错方式一致。
- **v5 UUID 极小概率碰撞**：身份派生冲突时服务端以 DB 记录指纹为准，碰撞表现为 409 而非错误数据。
- **第三方客户端重放 header**：统一使用 multipart 字段 `id`，不新增 header，绕开 CSRF/代理对自定义 header 的限制。

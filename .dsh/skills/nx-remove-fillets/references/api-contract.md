# 全局去圆角 Tool 接口

## `defeature_get_context`

输入：

```json
{}
```

返回当前 Session 绑定的 Job、Accepted Revision、Candidate Revision 和运行状态。

Agent 不得提供 Job ID。存在待审核 Candidate 时，不得执行新的删除操作。

## `defeature_rollback_accepted_revision`

将当前 Session 绑定 Job 的 Accepted Revision 回退到指定的严格祖先版本。

输入：

```json
{
  "target_revision": "r0"
}
```

安全约束：

- Job 和 Session 由服务端绑定，Agent 不得提交 Job ID、Session ID 或文件路径。
- 目标必须属于当前 Job，且必须是当前 Accepted Revision 的严格祖先。
- 存在待审核 Candidate 或 Job 正在处理时拒绝回退。
- 回退只移动 Accepted 指针并保留不可变版本历史；同时清空旧面选择并失效常驻 NX Runtime。

## `defeature_remove_all_fillets`

用于删除当前模型中全部或尽可能多的圆角。一次用户请求只调用一次。

输入：

```json
{
  "preference": "cliff"
}
```

参数：

- `preference`：可选，`cliff` 或 `notch`，默认 `cliff`。
- NX `DeletePartialBlend` 始终由后端关闭，不作为 Agent 参数。

Agent 不得提交 Revision、组件列表、STEP 面 ID、文件路径或 Journal。

## 返回结果

主要字段：

- `status`：`succeeded`、`no_components`、`no_deletable_components` 或 `aborted`。
- `source_revision`：本次操作所基于的 Accepted Revision。
- `candidate_revision`：成功时生成的待审核 Candidate，否则为 `null`。
- `successful_batches`：成功删除的组件批次摘要。
- `rounds`、`attempts`：诊断和用户汇总信息。
- `failure_category`、`error`：中止时的失败类别和错误。
- `all_failed`：是否检测到组件但没有任何组件能够删除。
- `roundtrip_normalization`：成功时的 NX 同进程最终结果规范化信息；仅在全部累计删除结束后执行一次，`scope` 为 `final-accumulated-result`，`reimport_count` 为 `1`。

结果处理：

- `succeeded`：向用户报告结果并等待其接受或拒绝 Candidate。
- `no_components`：报告没有检测到可处理组件。
- `no_deletable_components`：报告没有组件能够成功删除。
- `aborted`：报告错误类别和错误信息。

不要根据 `rounds`、`attempts` 或 `successful_batches` 发起额外检测、二分或删除调用。

## 指定单组件

用户明确指定 `fc-xxx` 时，使用 `nx-delete-fillet-component` Skill，而不是全局 Tool。

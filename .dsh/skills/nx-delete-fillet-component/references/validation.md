# 删除、回滚与最终交付

## 常驻 NX 单步删除

当前阶段单步删除成功条件只有一个：

```text
NX DeleteFaceBuilder.Commit() 未抛异常
```

成功时：

- 修改保留在 NX 内存中；
- `runtime_state` 增加一次；
- 所有旧组件、子段和面引用失效；
- 下一步必须重新分析。

失败时：

- Commit 异常被结构化返回；
- Worker 执行 Undo；
- Undo 后模型签名必须与操作前一致；
- `runtime_state` 不变；
- 只有普通几何失败才允许模型改换受控策略。

如果 Undo 或恢复验证失败，Worker 进入隔离状态并要求 reset。超时属于 ambiguous，不能按普通失败重新提交。

## 最终 Candidate 验证

单步 Commit 成功不等于最终交付验证成功。只有调用 `defeature_finish_and_publish` 时才执行：

1. 当前 NX 内存模型导出一次 STEP；
2. 输出文件存在且非空；
3. OCC 能重新读取并构建至少一个 BREP 面的 Mesh；
4. 验证完成后发布一个 Candidate Revision；
5. Accepted Revision 保持不变，等待用户审核。

最终验证失败时不得发布 Candidate，也不得自动接受或拒绝。

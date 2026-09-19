# 开发与测试

运行测试只需要 Python 标准库：

```bash
python3 tests/test_agent.py
python3 -m unittest discover -s tests -p 'test_portable.py' -v
python3 -m unittest discover -s tests -p 'test_syslog.py' -v
python3 scripts/check_repository.py
```

端口测试会在本机 loopback 短暂监听随机端口；文件变更和回滚测试使用临时目录及模拟 systemd，不安装服务。

创建可分发包：

```bash
python3 scripts/build_release.py
```

输出位于 dist/，包含 SHA256 校验文件。构建使用明确白名单，不打包站点配置、日志、数据库或安装审计。

CI 在 Debian 12/13 容器中运行单元测试、真实 rsyslog/logrotate 集成测试、隔离 loopback benchmark、隐私检查、发布包构建与配置语法检查。容器中的语法检查不等于 systemd 实机部署验收；实际服务器上仍需运行 `verify.py --network`，并从另一台 LAN 设备发送测试日志。

提交时请：

- 保持 Python 标准库实现和有界命令超时。
- 修改安装逻辑时覆盖配置保留、端口冲突和回滚路径。
- 示例使用 RFC 5737 文档网段；不要提交真实客户地址、主机名、账号、token、数据库或日志。
- 描述功能与验证结果，不在仓库中放入聊天记录、现场部署报告或个人机器路径。

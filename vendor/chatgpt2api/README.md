# 内置号池运行时

本目录是 ChatGPT2API 号池服务端的**代码快照**，随「Bluxe手绘工坊 · 生图工作台」插件一起分发，
安装后由工作台后端（pool_service.py）释放到本机数据目录并自建虚拟环境。

* 上游：MIT License，版权归原作者 kunkun，LICENSE 原文见 `source/LICENSE`。
* 已包含本工作台需要的两处补丁：round-robin 游标持久化、限流号排除；首次初始化时还会幂等复打一次。
* **不含**任何账号、token、密钥、代理配置或图片数据。`source/config.default.json` 是清洗后的出厂默认配置。
* 升级方式：改完本机号池源码后，重跑 `scripts/vendor-snapshot.py`，再重新发布插件。

分工：工作台插件负责「拉起并守护」号池进程；本目录只提供「能跑起来的代码」。
两者都不需要安装任何第三方 DSH 插件。

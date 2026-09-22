"""领域规则被违反时抛出的异常。"""


class DomainError(Exception):
    """所有领域错误的基类。"""


class ValidationError(DomainError):
    """数据不满足分项保存或必填要求。"""


class DuplicateSubmissionError(DomainError):
    """同一报名主体对同一作品的重复报名（业务冲突，区别于同源版本聚类）。"""


class MissingMaterialError(DomainError):
    """影片尚未可评审：仍有必交材料缺失。"""


class AuthorizationError(DomainError):
    """评委试图接触未获放映授权的影片文件。"""


class ConflictError(DomainError):
    """存在未解决的回避关系，禁止分配评审任务。"""


class LifecycleError(DomainError):
    """状态机不允许的操作（如撤回后再换版、未完成复核就公布）。"""


class SecrecyError(DomainError):
    """正式通知前查询或泄露处于隔离期的审定结果。"""


class AuditError(DomainError):
    """审计日志断裂或被篡改。"""

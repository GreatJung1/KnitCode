class Context:
    def invoke(self):
        return "ok"

    def fail(self):
        raise RuntimeError('demo')


class Other:
    def invoke(self):
        return "other"

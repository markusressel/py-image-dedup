class Action:
    def __init__(self, name, color, ):
        self.name = name
        self.color = color


class ActionEnum:
    NONE = Action("-", "green")
    DELETE = Action("delete", "red")
    MOVE = Action("move", "yellow")

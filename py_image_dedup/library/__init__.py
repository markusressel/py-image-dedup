class Action:
    def __init__(self, name, color, ):
        self.name = name
        self.color = color


class ActionEnum:
    NONE = Action("-", "green")
    REMOVE = Action("remove", "red")
    MOVE = Action("move", "yellow")

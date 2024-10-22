import rospy
import skiros2_msgs.msg as msgs
import skiros2_msgs.srv as srvs
from std_srvs.srv import Trigger, TriggerResponse
import skiros2_common.ros.utils as utils
from skiros2_skill.ros.utils import *
import skiros2_world_model.ros.world_model_interface as wmi
import skiros2_skill.core.skill as skill
from skiros2_common.core.abstract_skill import State
from skiros2_skill.core.skill_instanciator import SkillInstanciator
from .discovery_interface import DiscoverableNode
import skiros2_common.tools.logger as log
from skiros2_common.core.world_element import Element
from skiros2_common.tools.id_generator import IdGen
from multiprocessing.dummy import Process
import skiros2_skill.core.visitors as visitors
from std_msgs.msg import Empty, Bool
import inflection  # For camel-snake case conversion

log.setLevel(log.INFO)


def skill2msg(skill):
    msg = msgs.ResourceDescription()
    msg.type = skill.type
    msg.name = skill.label
    msg.params = utils.serializeParamMap(skill._description._params.getParamMap())
    return msg


class BtTicker:
    """
    Manager of a set of Behavior Trees (Tasks) and a visitor

    Ticks the tasks sequentially, with the specified visitor

    Provides interfaces to start, pause, stop the ticking process and to add/remove tasks
    """
    _verbose = True
    _tasks_to_preempt = list()
    _tasks_to_pause = dict()
    _tasks = {}
    _process = None
    _visitor = None
    _id_gen = IdGen()

    _progress_cb = None
    _tick_cb = None

    _finished_skill_ids = dict()

    def _run(self, _):
        """
        @brief Tick tasks at 25hz
        """
        BtTicker._finished_skill_ids = dict()
        rate = rospy.Rate(25)
        log.info("[BtTicker]", "Execution starts.")
        while BtTicker._tasks:
            self._tick()
            rate.sleep()
            self._tick_cb()
        log.info("[BtTicker]", "Execution stops.")

    def _tick(self):
        visitor = BtTicker._visitor
        printer = visitors.VisitorPrint(BtTicker._visitor._wm, BtTicker._visitor._instanciator)
        for uid in list(BtTicker._tasks.keys()):
            if uid in BtTicker._tasks_to_preempt:
                BtTicker._tasks_to_preempt.remove(uid)
                visitor.preempt()
            if uid in BtTicker._tasks_to_pause.keys():
                if BtTicker._tasks_to_pause[uid] > 0:
                    BtTicker._tasks_to_pause[uid] -= 1
                else:
                    continue
            t = BtTicker._tasks[uid]
            result = visitor.traverse(t)
            printer.traverse(t)
            self.publish_progress(uid, printer)
            if result != State.Running and result != State.Idle:
                self.remove_task(uid)

    def kill(self):
        if BtTicker._process is not None:
            del BtTicker._process
            BtTicker._process = None
            self._tick()

    def is_running(self):
        if BtTicker._process is None:
            return False
        return BtTicker._process.is_alive()

    def publish_progress(self, uid, visitor):
        self._progress_cb(task_id=uid, tree=visitor.snapshot())

    def observe_progress(self, func):
        self._progress_cb = func

    def observe_tick(self, func):
        self._tick_cb = func

    def clear(self):
        if BtTicker._visitor:
            BtTicker._visitor.preempt()
            BtTicker._process.join()
            BtTicker._visitor = None
        BtTicker._tasks.clear()
        BtTicker._id_gen.clear()

    def add_task(self, obj, desired_id=-1):
        uid = BtTicker._id_gen.getId(desired_id)
        obj._label = "task_{}".format(uid)
        BtTicker._tasks[uid] = obj
        return uid

    def remove_task(self, uid):
        BtTicker._tasks.pop(uid)
        BtTicker._id_gen.removeId(uid)

    def start(self, visitor, uid):
        if uid in BtTicker._tasks_to_pause:
            log.info("[start]", "Resuming task {}.".format(uid))
            del BtTicker._tasks_to_pause[uid]
        else:
            log.info("[start]", "Starting task {}.".format(uid))
        if not self.is_running():
            BtTicker._visitor = visitor
            BtTicker._process = Process(target=BtTicker._run, args=(self, True))
            BtTicker._process.start()
            return True

    def join(self):
        BtTicker._process.join()

    def pause(self, uid):
        log.info("[pause]", "Pausing task {}.".format(uid))
        BtTicker._tasks_to_pause[uid] = 0

    def tick_once(self, uid):
        log.info("[tick_once]", "Tick once task {}.".format(uid))
        BtTicker._tasks_to_pause[uid] = 1

    def preempt(self, uid):
        log.info("[preempt]", "Stopping task {}...".format(uid))
        if uid in BtTicker._tasks_to_pause:
            del BtTicker._tasks_to_pause[uid]
        BtTicker._tasks_to_preempt.append(uid)
        starttime = rospy.Time.now()
        timeout = rospy.Duration(5.0)
        while(self.is_running() and rospy.Time.now() - starttime < timeout):
            rospy.sleep(0.01)
        if self.is_running():
            log.info("preempt", "Task {} is not answering. Killing process.".format(uid))
            self.kill()
        log.info("preempt", "Task {} preempted.".format(uid))

    def preempt_all(self):
        for uid in list(BtTicker._tasks.keys()):
            self.preempt(uid)

    def pause_all(self):
        for uid in list(BtTicker._tasks.keys()):
            self.pause(uid)

    def tick_once_all(self):
        for uid in list(BtTicker._tasks.keys()):
            self.tick_once(uid)


class SkillManager:
    """
    @brief The skill manager manage a sub-system of the robot
    """

    def __init__(self, prefix, agent_name, verbose=True):
        self._agent_name = agent_name
        self._wmi = wmi.WorldModelInterface(agent_name, make_cache=True)
        self._wmi.set_default_prefix(prefix)
        self._local_wm = self._wmi
        self._instanciator = SkillInstanciator(self._local_wm)
        self._ticker = BtTicker()
        self._verbose = verbose
        self._ticker._verbose = verbose
        self._register_agent(agent_name)
        self._skills = []
        # self._wmi.unlock() #Ensures the world model's mutex is unlocked

    @property
    def skills(self):
        return self._skills

    def observe_task_progress(self, func):
        self._ticker.observe_progress(func)

    def observe_tick(self, func):
        self._ticker.observe_tick(func)

    def _register_agent(self, agent_name):
        res = self._wmi.resolve_element(Element("cora:Robot", agent_name))
        if res:
            log.info("[{}]".format(self.__class__.__name__), "Found robot {}, skipping registration.".format(res))
            self._robot = res
            for r in self._robot.getRelations("-1", "skiros:hasSkill"):
                self._wmi.remove_element(self._wmi.get_element(r['dst']))
            self._robot = self._wmi.get_element(self._robot.id)
        else:
            self._robot = self._wmi.instanciate(agent_name, True)
            startLocUri = self._wmi.get_template_element(agent_name).getRelations(pred="skiros:hasStartLocation")
            if startLocUri:
                start_location = self._wmi.instanciate(startLocUri[0]["dst"], False, [])
                self._wmi.set_relation(self._robot._id, "skiros:at", start_location._id)
                self._robot = self._wmi.get_element(self._robot.id)
        log.info("[{}]".format(self.__class__.__name__), "Registered robot {}".format(self._robot))
        self._robot.setProperty("skiros:SkillMgr", self._agent_name[self._agent_name.rfind(":") + 1:])
        self._wmi.update_element(self._robot)

    def shutdown(self):
        for s in self.skills:
            self._wmi.remove_element(s)
        self._wmi.unlock()  # Ensures the world model's mutex gets unlocked

    def load_skills(self, package):
        """
        Load definitions from a package
        """
        self._instanciator.load_library(package, self._verbose)

    def is_skill_in_world_model(self, name):
        """
        @brief Check if a skill is already in the world model
        """
        scene = self._wmi.get_scene()[0]
        for e in scene:
            if e.label == name:
                return True
        return False

    def add_skill(self, name, subclass="skiros:CompoundSkill"):
        """
        @brief Add a skill to the available skill set
        """
        skill = self._instanciator.add_instance(name)

        # Don't add duplicates in the skill list
        if skill not in self._skills:
            self._skills.append(skill)

        # Don't add duplicates in the world model
        if self.is_skill_in_world_model(name):
            return

        e = skill.toElement()
        e.addRelation(self._robot._id, "skiros:hasSkill", "-1")
        # print skill.printInfo(True)
        hierarchy = skill.__module__.split(".") + [e.type]
        for c1, c2 in zip([None] + hierarchy[:-1], hierarchy):
            if c1 is None:
                c1 = "skiros:Skill"
            c1 = c1 if c1.find(":") > 0 else "skiros:{}".format(inflection.camelize(c1))
            c2 = c2 if c2.find(":") > 0 else "skiros:{}".format(inflection.camelize(c2))
            if not self._wmi.get_type(c2):
                self._wmi.add_class(c2, c1)
        self._wmi.add_element(e)
        self._skills.append(skill)
        return SkillHolder(self._agent_name, skill.type, skill.label, skill.params.getCopy())

    def add_primitive(self, name):
        """
        @brief Add a local primitive
        """
        self.add_skill(name, "skiros:PrimitiveSkill")

    def add_task(self, task):
        """
        @brief Add a new task to the list
        """
        root = skill.Root("root", self._local_wm)
        for i in task:
            log.info("[SkillManager]", "Add task {}:{} \n {}".format(i.type, i.name, i.ph.printState()))
            root.addChild(skill.SkillWrapper(i.type, i.name, self._instanciator))
            root.last().specifyParamsDefault(i.ph)
        return self._ticker.add_task(root, root.id)

    def preempt_task(self, uid):
        """
        @brief Preempt a task
        """
        if uid == -1:
            self._ticker.preempt_all()
        else:
            self._ticker.preempt(uid)

    def pause(self, uid):
        """
        @brief Stop ticking a task, but do not preempt it
        """
        if uid == -1:
            self._ticker.pause_all()
        else:
            self._ticker.pause(uid)

    def tick_once(self, uid):
        """
        @brief Set a task to go in pause state after 1 tick
        """
        if uid == -1:
            self._ticker.tick_once_all()
        else:
            self._ticker.tick_once(uid)

    def print_task(self, uid):
        self.visitor = visitors.VisitorPrint(self._local_wm, self._instanciator)
        self.visitor.setVerbose(self._verbose)
        return self._ticker.start(self.visitor, uid)

    def execute_task(self, uid, sim=False, track_params=list()):  # [("MotionChange",)]
        """
        @brief Start or continue a task execution
        """
        self.visitor = visitors.VisitorExecutor(self._local_wm, self._instanciator)
        self.visitor.setSimulate(sim)
        for t in track_params:
            self.visitor.trackParam(*t)
        self.visitor.setVerbose(self._verbose)
        return self._ticker.start(self.visitor, uid)

    def clear_tasks(self):
        self._ticker.clear()

    def execute_optimal(self):
        # Optimize Procedure
        self.optimizeTask()
        self.print_task()
        # Execute
        return self.execute_task(False)

    def simulate_task(self, uid):
        self.visitor = visitors.VisitorReversibleSimulator(self._local_wm, self._instanciator)
        self.visitor.setVerbose(self._verbose)
        # self.visitor.trackParam("Initial")
        # self.visitor.trackParam("Gripper")
        if self.visitor.traverse(self._tasks[uid]):
            self._task = self.visitor.getExecutionRoot()

    def optimize_task(self):
        self.visitor = optimizer.VisitorOptimizer(self._local_wm, self._instanciator)
        # self.visitor.setVerbose(True)
        # self.visitor.trackParam("PlacingCell")
        # self.visitor.trackParam("Object")
        # rospy.sleep(1.)
        self.publish("Optimization", 1, "Start.")
        try:
            if self.visitor.traverse(self._task):
                self._task = self.visitor.getExecutionRoot()
                return True
            else:
                self._task = self.visitor.getExecutionRoot()
                return False
        except KeyError as e:
            self._task = self.visitor.getExecutionRoot()
            print("Exe: {}".format(self.visitor._execution_branch))
            self.print_task()
            raise e


class SkillManagerNode(DiscoverableNode):
    """
    At boot:
        -add the robot description on the world model
        -load skills and add descriptions on the world model
        -interfaces with resource mgrs and retrieve resource list
        -add resource descriptions on the world model
    Main roles:
        -receive tasks (sequences of skills) and process them with visitors
        -currently available visitors: print, execute, simulate, optimize
        -publish feedback on topic /monitor
    """

    def __init__(self):
        rospy.init_node("skill_mgr", anonymous=False)
        self.publish_runtime_parameters = False
        robot_name = rospy.get_name()
        prefix = ""
        full_name = rospy.get_param('~prefix', prefix) + ':' + robot_name[robot_name.rfind("/") + 1:]
        self.sm = SkillManager(rospy.get_param('~prefix', prefix), full_name, verbose=rospy.get_param('~verbose', True))
        self.sm.observe_task_progress(self._on_progress_update)
        self.sm.observe_tick(self._on_tick)

        # Init skills
        self._initialized = False
        self._getskills = rospy.Service('~get_skills', srvs.ResourceGetDescriptions, self._get_descriptions_cb)
        self._init_skills()
        rospy.sleep(0.5)
        self._initialized = True

        # Start communications
        self._update_skills = rospy.Service('~update_skills', Trigger, self._update_skills_cb)
        self._command = rospy.Service('~command', srvs.SkillCommand, self._command_cb)
        self._monitor = rospy.Publisher("~monitor", msgs.TreeProgress, queue_size=20)
        self._tick_rate = rospy.Publisher("~tick_rate", Empty, queue_size=20)
        self._set_debug = rospy.Subscriber('~set_debug', Bool, self._set_debug_cb)
        rospy.on_shutdown(self.shutdown)
        self.init_discovery("skill_managers", robot_name)
        log.info("[{}]".format(rospy.get_name()), "Skill manager ready.")

    def _set_debug_cb(self, msg):
        self.publish_runtime_parameters = msg.data

    def _update_skills_cb(self, _):
        '''
        Callback function for the update_skills service. 
        '''
        self.sm.skills.clear()
        self._init_skills()

        res = TriggerResponse()
        res.success = True
        return res

    def _init_skills(self):
        """
        @brief Initialize the robot with a set of skills
        """
        for r in rospy.get_param('~libraries_list', []):
            log.info("[LoadLibrary]", str(r))
            self.sm.load_skills(r)

        for r in rospy.get_param('~primitive_list', []):
            log.info("[LoadPrimitive]", str(r))
            self.sm.add_primitive(r)

        sl = rospy.get_param('~skill_list', [])
        for r in sl:
            log.info("[LoadSkill]", str(r))
            self.sm.add_skill(r)

    def _make_task(self, msg):
        task = []
        for s in msg:
            task.append(SkillHolder("", s.type, s.name, utils.deserializeParamMap(s.params)))
        return task

    def _command_cb(self, msg):
        """
        @brief Commands execution of skills
        """
        task_id = msg.execution_id
        if msg.action == msg.START:
            if task_id == -1:
                task_id = self.sm.add_task(self._make_task(msg.skills))
            self.sm.execute_task(task_id)
        elif msg.action == msg.PREEMPT:
            task_id = self.sm.preempt_task(msg.execution_id)
        elif msg.action == msg.PAUSE:
            task_id = self.sm.pause(msg.execution_id)
        elif msg.action == msg.TICK_ONCE:
            if task_id == -1:
                task_id = self.sm.add_task(self._make_task(msg.skills))
                self.sm.execute_task(task_id)
            self.sm.tick_once(task_id)
        else:
            log.error("[{}]".format(self.__class__.__name__), "Unrecognized command.")
            return srvs.SkillCommandResponse(False, -1)
        return srvs.SkillCommandResponse(True, task_id)

    def _on_tick(self):
        """
        @brief To measure the tick rate externally
        """
        self._tick_rate.publish(Empty())

    def _on_progress_update(self, *args, **kwargs):
        """
        @brief Publish all skill progress
        """
        task_id = kwargs['task_id']
        tree = kwargs['tree']
        messages = msgs.TreeProgress()
        for (idd, desc) in tree:
            log.debug("[{}]".format(self.__class__.__name__),
                      "{}:Task[{task_id}]{type}:{label}[{id}]: Message[{code}]: {msg} ({state})".format(
                      self.sm._agent_name[1:], task_id=task_id, id=idd, **desc))
            msg = msgs.SkillProgress()
            msg.robot = rospy.get_name()
            msg.task_id = task_id
            msg.id = idd
            msg.type = desc['type']
            msg.label = desc['label']
            if self.publish_runtime_parameters:
                msg.params = utils.serializeParamMap(desc['params'])
            msg.state = desc['state'].value
            msg.processor = desc['processor']
            msg.parent_label = desc['parent_label']
            msg.parent_id = desc['parent_id']
            msg.progress_code = desc['code']
            msg.progress_period = desc['period']
            msg.progress_time = desc['time']
            msg.progress_message = desc['msg']

            messages.progress.append(msg)
        self._monitor.publish(messages)

    def _get_descriptions_cb(self, msg):
        """
        @brief Returns available skills. Called when receiving a command on ~/get_descriptions
        """
        while not self._initialized:
            rospy.sleep(0.1)
        to_ret = srvs.ResourceGetDescriptionsResponse()
        for s in self.sm.skills:
            to_ret.list.append(skill2msg(s))
        return to_ret

    def shutdown(self):
        self.sm.shutdown()

    def run(self):
        rospy.spin()


if __name__ == '__main__':
    node = SkillManagerNode()
    node.run()

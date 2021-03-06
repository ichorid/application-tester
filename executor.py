import logging
import os
import subprocess
from bisect import bisect
from random import random, randint, choice

import time

import sys
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.internet.task import LoopingCall

from actions.browse_discovered_action import BrowseDiscoveredAction
from actions.change_anonymity_action import ChangeAnonymityAction
from actions.check_crash_action import CheckCrashAction
from actions.explore_channel_action import ExploreChannelAction
from actions.explore_download_action import ExploreDownloadAction
from actions.page_action import RandomPageAction
from actions.screenshot_action import ScreenshotAction
from actions.search_action import RandomSearchAction
from actions.shutdown_action import ShutdownAction
from actions.start_download_action import StartRandomDownloadAction
from actions.remove_download_action import RemoveRandomDownloadAction
from actions.start_vod_action import StartVODAction
from actions.subscribe_unsubscribe_action import SubscribeUnsubscribeAction
from actions.wait_action import WaitAction
from download_monitor import DownloadMonitor
from ircclient import IRCManager
from requestmgr import HTTPRequestManager
from resource_monitor import ResourceMonitor


class Executor(object):

    def __init__(self, args):
        self.tribler_path = args.tribler_executable
        self._logger = logging.getLogger(self.__class__.__name__)
        self.allow_plain_downloads = args.plain
        self.magnets_file_path = args.magnetsfile
        self.pending_tasks = {}  # Dictionary of pending tasks
        self.probabilities = []

        if not args.silent:
            self.random_action_lc = LoopingCall(self.perform_random_action)
            self.random_action_lc.start(15)
        else:
            # Trigger Tribler startup through a simple action
            self.execute_action(WaitAction(1000))

        self.check_task_completion_lc = LoopingCall(self.check_task_completion)
        self.check_task_completion_lc.start(2, now=False)

        self.check_crash_lc = LoopingCall(self.check_crash)
        self.check_crash_lc.start(11, now=False)

        self.start_time = time.time()
        self.request_manager = HTTPRequestManager()
        self.irc_manager = None
        self.tribler_crashed = False
        self.download_monitor = None

        if args.ircid:
            self.irc_manager = IRCManager(self, args.ircid)
            self.irc_manager.start()

        if args.duration:
            reactor.callLater(args.duration, self.stop, 0)

        if args.monitordownloads:
            self.download_monitor = DownloadMonitor(args.monitordownloads)
            reactor.callLater(20, self.download_monitor.start)

        if args.monitorresources:
            self.resource_monitor = ResourceMonitor(args.monitorresources)
            reactor.callLater(20, self.resource_monitor.start)

        # Determine probabilities
        with open(os.path.join(os.getcwd(), "data", "action_weights.txt"), "r") as action_weights_file:
            content = action_weights_file.read()
            for line in content.split('\n'):
                if len(line) == 0:
                    continue

                if line.startswith('#'):
                    continue

                parts = line.split('=')
                if len(parts) < 2:
                    continue

                self.probabilities.append((parts[0], int(parts[1])))

    def stop(self, exit_code):
        # Stop the execution of random actions and send a message to the IRC
        self.random_action_lc.stop()
        self.check_crash_lc.stop()

        def on_tribler_shutdown(_):
            reactor.stop()
            try:
                sys.exit(exit_code)
            except SystemExit:
                pass

        reactor.callLater(20, on_tribler_shutdown, None)  # Give it 20 seconds to shutdown

        self.execute_action(ShutdownAction()).addCallback(on_tribler_shutdown)

    @property
    def uptime(self):
        return time.time() - self.start_time

    def check_crash(self):
        """
        Check whether the Tribler instance has crashed.
        """
        def on_crash_result(result):
            if result:
                self._logger.error("Tribler crashed after uptime of %s sec! Stack trace: %s", self.uptime, result)
                self.tribler_crashed = True
                if self.irc_manager:
                    self.irc_manager.irc.send_channel_message("Tribler crashed with stack trace: %s" % result)
                self.stop(1)

        self.execute_action(CheckCrashAction()).addCallback(on_crash_result)

    def check_task_completion(self):
        """
        This method periodically checks whether Python scripts have been completed.
        Completion of such a script is indicated by presence of a .done file.
        """
        for done_file_name in os.listdir(os.path.join(os.getcwd(), "tmp_scripts")):
            if done_file_name.endswith(".done"):
                task_id = done_file_name[:-5]
                self._logger.info("Task with ID %s completed!", task_id)

                # Read the contents of the file
                file_content = None
                with open(os.path.join(os.getcwd(), "tmp_scripts", done_file_name)) as done_file:
                    file_content = done_file.read()

                if not file_content:
                    file_content = None

                # Invoke the callback
                if task_id in self.pending_tasks:
                    self.pending_tasks[task_id].callback(file_content)
                    self.pending_tasks.pop(task_id, None)

                os.remove(os.path.join(os.getcwd(), "tmp_scripts", done_file_name))

                python_file_path = os.path.join(os.getcwd(), "tmp_scripts", "%s.py" % task_id)
                if os.path.exists(python_file_path):
                    os.remove(python_file_path)

    def weighted_choice(self, choices):
        if len(choices) == 0:
            return None
        values, weights = zip(*choices)
        total = 0
        cum_weights = []
        for w in weights:
            total += w
            cum_weights.append(total)
        x = random() * total
        i = bisect(cum_weights, x)
        return values[i]

    def get_rand_bool(self):
        return randint(0, 1) == 0

    def execute_action(self, action):
        """
        Execute a given action and return a deferred that fires with the result of the action.
        """
        self._logger.info("Executing action: %s" % action)

        task_id = ''.join(choice('0123456789abcdef') for _ in xrange(10))
        task_deferred = Deferred()
        self.pending_tasks[task_id] = task_deferred

        tmp_scripts_dir = os.path.join(os.getcwd(), "tmp_scripts")
        if not os.path.exists(tmp_scripts_dir):
            os.makedirs(tmp_scripts_dir)
        code_file_path = os.path.join(tmp_scripts_dir, "%s.py" % task_id)

        # First, write a function to end the program to the file
        destination = os.path.join(tmp_scripts_dir, "%s.done" % task_id)
        if os.name == 'nt':
            destination = destination.replace('\\', '\\\\')

        code = """return_value = ''

def exit_script():
    import sys
    global return_value
    print 'Done with task %s, writing .done file'
    with open('%s', 'a') as done_file:
        done_file.write(return_value)
    sys.exit(0)\n\n""" % (task_id, destination)

        code += action.generate_code() + '\nexit_script()'

        # Write the generated code to a separate file
        with open(code_file_path, "wb") as code_file:
            code_file.write(code)

        # Let Tribler execute this code
        self.execute_code(code_file_path)

        return task_deferred

    def execute_code(self, code_file_path):
        self._logger.info("Executing code file: %s" % code_file_path)
        subprocess.Popen("%s \"code:%s\"" % (self.tribler_path, code_file_path), shell=True)

    def perform_random_action(self):
        """
        This method performs a random action in Tribler.
        There are various actions possible that can occur with different probabilities.
        """
        action = self.weighted_choice(self.probabilities)
        if not action:
            self._logger.warning("No action available!")
            self.execute_action(WaitAction(1000))
            return
        self._logger.info("Performing action: %s", action)
        if action == 'random_page':
            action = RandomPageAction()
        elif action == 'search':
            action = RandomSearchAction()
        elif action == 'start_download':
            action = StartRandomDownloadAction(self.magnets_file_path)
        elif action == 'remove_download':
            action = RemoveRandomDownloadAction()
        elif action == 'explore_download':
            action = ExploreDownloadAction()
        elif action == 'browse_discovered':
            action = BrowseDiscoveredAction()
        elif action == 'explore_channel':
            action = ExploreChannelAction()
        elif action == 'screenshot':
            action = ScreenshotAction()
        elif action == 'start_vod':
            action = StartVODAction()
        elif action == 'change_anonymity':
            action = ChangeAnonymityAction(allow_plain=self.allow_plain_downloads)
        elif action == 'subscribe_unsubscribe':
            action = SubscribeUnsubscribeAction()

        self.execute_action(action)

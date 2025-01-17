#!/usr/bin/python

from __future__ import print_function

'''
log analysis program
Andrew Tridgell December 2014
'''

import sys, struct, time, os, datetime, platform
import math, re
import fnmatch
import threading
import shlex
from math import *
from MAVProxy.modules.lib import multiproc
from MAVProxy.modules.lib import rline
from MAVProxy.modules.lib import wxconsole
from MAVProxy.modules.lib.graph_ui import Graph_UI
from pymavlink.mavextra import *
from MAVProxy.modules.lib.mp_menu import *
import MAVProxy.modules.lib.mp_util as mp_util
from pymavlink import mavutil
from MAVProxy.modules.lib.mp_settings import MPSettings, MPSetting
from MAVProxy.modules.lib import wxsettings
from MAVProxy.modules.lib.graphdefinition import GraphDefinition
from lxml import objectify
import pkg_resources
from builtins import input

grui = []
last_xlim = None
flightmodes = None

#Global var to hold the GUI menu element
TopMenu = None

class MEStatus(object):
    '''status object to conform with mavproxy structure for modules'''
    def __init__(self):
        self.msgs = {}

class MEState(object):
    '''holds state of MAVExplorer'''
    def __init__(self):
        self.input_queue = multiproc.Queue()
        self.rl = None
        self.console = wxconsole.MessageConsole(title='MAVExplorer')
        self.exit = False
        self.status = MEStatus()
        self.settings = MPSettings(
            [ MPSetting('marker', str, '+', 'data marker', tab='Graph'),
              MPSetting('condition', str, None, 'condition'),
              MPSetting('xaxis', str, None, 'xaxis'),
              MPSetting('linestyle', str, None, 'linestyle'),
              MPSetting('show_flightmode', bool, True, 'show flightmode'),
              MPSetting('sync_xzoom', bool, True, 'sync X-axis zoom'),
              MPSetting('legend', str, 'upper left', 'legend position'),
              MPSetting('legend2', str, 'upper right', 'legend2 position')
              ]
            )

        self.mlog = None
        self.filename = None
        self.command_map = command_map
        self.completions = {
            "set"       : ["(SETTING)"],
            "condition" : ["(VARIABLE)"],
            "graph"     : ['(VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE)'],
            "map"       : ['(VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE) (VARIABLE)']
            }
        self.aliases = {}
        self.graphs = []
        self.flightmode_selections = []
        self.last_graph = GraphDefinition('Untitled', '', '', [], None)
        
        #pipe to the wxconsole for any child threads (such as the save dialog box)
        self.parent_pipe_recv_console,self.child_pipe_send_console = multiproc.Pipe(duplex=False)
        #pipe for creating graphs (such as from the save dialog box)
        self.parent_pipe_recv_graph,self.child_pipe_send_graph = multiproc.Pipe(duplex=False)
        
        tConsoleWrite = threading.Thread(target=self.pipeRecvConsole)
        tConsoleWrite.daemon = True
        tConsoleWrite.start()
        tGraphWrite = threading.Thread(target=self.pipeRecvGraph)
        tGraphWrite.daemon = True
        tGraphWrite.start()
                
    def pipeRecvConsole(self):
        '''watch for piped data from save dialog'''
        try:
            while True:
                console_msg = self.parent_pipe_recv_console.recv()
                if console_msg is not None:
                    self.console.writeln(console_msg)
                time.sleep(0.1)
        except EOFError:
            pass

    def pipeRecvGraph(self):
        '''watch for piped data from save dialog'''
        try:
            while True:
                graph_rec = self.parent_pipe_recv_graph.recv()
                if graph_rec is not None:
                    mestate.input_queue.put(graph_rec)
                time.sleep(0.1)
        except EOFError:
            pass
            
def have_graph(name):
    '''return true if we have a graph of the given name'''
    for g in mestate.graphs:
        if g.name == name:
            return True
    return False

def menu_callback(m):
    '''called on menu selection'''
    if m.returnkey.startswith('# '):
        cmd = m.returnkey[2:]
        if m.handler is not None:
            if m.handler_result is None:
                return
            cmd += m.handler_result
        process_stdin(cmd)
    elif m.returnkey == 'menuSettings':
        wxsettings.WXSettings(mestate.settings)
    elif m.returnkey.startswith("mode-"):
        idx = int(m.returnkey[5:])
        mestate.flightmode_selections[idx] = m.IsChecked()
    elif m.returnkey.startswith("loadLog"):
        print("File: " + m.returnkey[8:])
    elif m.returnkey == 'quit':
        mestate.console.close()
        mestate.exit = True
        print("Exited. Press Enter to continue.")
        sys.exit(0)

    else:
        print('Unknown menu selection: %s' % m.returnkey)


def flightmode_menu():
    '''construct flightmode menu'''
    global flightmodes
    ret = []
    idx = 0
    for (mode,t1,t2) in flightmodes:
        modestr = "%s %us" % (mode, (t2-t1))
        ret.append(MPMenuCheckbox(modestr, modestr, 'mode-%u' % idx))
        idx += 1
        mestate.flightmode_selections.append(False)
    return ret


def graph_menus():
    '''return menu tree for graphs (recursive)'''
    ret = MPMenuSubMenu('Graphs', [])
    for i in range(len(mestate.graphs)):
        g = mestate.graphs[i]
        path = g.name.split('/')
        name = path[-1]
        path = path[:-1]
        ret.add_to_submenu(path, MPMenuItem(name, name, '# graph :%u' % i))
    return ret

def setup_file_menu():
    global TopMenu
    TopMenu = MPMenuTop([])
    TopMenu.add(MPMenuSubMenu('MAVExplorer',
                           items=[MPMenuItem('Settings', 'Settings', 'menuSettings'),
                                  MPMenuItem('&Open\tCtrl+O', 'Open Log', '# loadLog ',
                                            handler=MPMenuCallFileDialog(
                                                                        flags=('open',),
                                                                        title='Logfile Load',
                                                                        wildcard='*.tlog;*.log;*.BIN;*.bin')),
                                  MPMenuItem('&Quit\tCtrl+Q', 'Quit', 'quit')]))
    mestate.console.set_menu(TopMenu, menu_callback)

def setup_menus():
    '''setup console menus'''
    global TopMenu
    TopMenu.add(MPMenuSubMenu('Display',
                           items=[MPMenuItem('Map', 'Map', '# map'),
                                  MPMenuItem('Save Graph', 'Save', '# save'),
                                  MPMenuItem('Reload Graphs', 'Reload', '# reload'),
                                  MPMenuItem('FFT', 'FFT', '# fft')]))
    TopMenu.add(graph_menus())
    TopMenu.add(MPMenuSubMenu('FlightMode', items=flightmode_menu()))

    mestate.console.set_menu(TopMenu, menu_callback)

def expression_ok(expression, msgs=None):
    '''return True if an expression is OK with current messages'''
    expression_ok = True
    fields = expression.split()
    if msgs is None:
        msgs = mestate.status.msgs
    for f in fields:
        try:
            if f.endswith(':2'):
                f = f[:-2]
            if mavutil.evaluate_expression(f, msgs) is None:
                expression_ok = False
        except Exception:
            expression_ok = False
            break
    return expression_ok

def load_graph_xml(xml, filename, load_all=False):
    '''load a graph from one xml string'''
    ret = []
    try:
        root = objectify.fromstring(xml)
    except Exception:
        return []
    if root.tag != 'graphs':
        return []
    if not hasattr(root, 'graph'):
        return []
    for g in root.graph:
        name = g.attrib['name']
        expressions = [e.text for e in g.expression]
        if load_all:
            ret.append(GraphDefinition(name, e, g.description.text, expressions, filename))
            continue
        if have_graph(name):
            continue
        for e in expressions:
            if expression_ok(e):
                ret.append(GraphDefinition(name, e, g.description.text, expressions, filename))
                break
    return ret

def load_graphs():
    '''load graphs from mavgraphs.xml'''
    mestate.graphs = []
    gfiles = ['mavgraphs.xml']
    if 'HOME' in os.environ:
        for dirname, dirnames, filenames in os.walk(os.path.join(os.environ['HOME'], ".mavproxy")):
            for filename in filenames:
                if filename.lower().endswith('.xml'):
                    gfiles.append(os.path.join(dirname, filename))
    elif 'LOCALAPPDATA' in os.environ:
        for dirname, dirnames, filenames in os.walk(os.path.join(os.environ['LOCALAPPDATA'], "MAVProxy")):
            for filename in filenames:
                if filename.lower().endswith('.xml'):
                    gfiles.append(os.path.join(dirname, filename))

    for file in gfiles:
        if not os.path.exists(file):
            continue
        graphs = load_graph_xml(open(file).read(), file)
        if graphs:
            mestate.graphs.extend(graphs)
            mestate.console.writeln("Loaded %s" % file)
    # also load the built in graphs
    try:
        dlist = pkg_resources.resource_listdir("MAVProxy", "tools/graphs")
        for f in dlist:
            raw = pkg_resources.resource_stream("MAVProxy", "tools/graphs/%s" % f).read()
            graphs = load_graph_xml(raw, None)
            if graphs:
                mestate.graphs.extend(graphs)
                mestate.console.writeln("Loaded %s" % f)
    except Exception:
        #we're in a Windows exe, where pkg_resources doesn't work
        import pkgutil
        for f in ["ekf3Graphs.xml", "ekfGraphs.xml", "mavgraphs.xml", "mavgraphs2.xml"]:
            raw = pkgutil.get_data( 'MAVProxy', 'tools//graphs//' + f)
            graphs = load_graph_xml(raw, None)
            if graphs:
                mestate.graphs.extend(graphs)
                mestate.console.writeln("Loaded %s" % f)
    mestate.graphs = sorted(mestate.graphs, key=lambda g: g.name)

def flightmode_colours():
    '''return mapping of flight mode to colours'''
    from MAVProxy.modules.lib.grapher import flightmode_colours
    mapping = {}
    idx = 0
    for (mode,t0,t1) in flightmodes:
        if not mode in mapping:
            mapping[mode] = flightmode_colours[idx]
            idx += 1
            if idx >= len(flightmode_colours):
                idx = 0
    return mapping

def cmd_graph(args):
    '''graph command'''
    usage = "usage: graph <FIELD...>"
    if len(args) < 1:
        print(usage)
        return
    if args[0][0] == ':':
        i = int(args[0][1:])
        g = mestate.graphs[i]
        expression = g.expression
        args = expression.split()
        mestate.console.write("Added graph: %s\n" % g.name)
        if g.description:
            mestate.console.write("%s\n" % g.description, fg='blue')
        mestate.rl.add_history("graph %s" % ' '.join(expression.split()))
        mestate.last_graph = g
    else:
        expression = ' '.join(args)
        mestate.last_graph = GraphDefinition('Untitled', expression, '', [expression], None)
    grui.append(Graph_UI(mestate))
    grui[-1].display_graph(mestate.last_graph, flightmode_colours())
    global last_xlim
    if last_xlim is not None and mestate.settings.sync_xzoom:
        #print("initial: ", last_xlim)
        grui[-1].set_xlim(last_xlim)

def cmd_map(args):
    '''map command'''
    import mavflightview
    #mestate.mlog.reduce_by_flightmodes(mestate.flightmode_selections)
    #setup and process the map
    options = mavflightview.mavflightview_options()
    options.condition = mestate.settings.condition
    options._flightmodes = mestate.mlog._flightmodes
    options.show_flightmode_legend = mestate.settings.show_flightmode
    if len(args) > 0:
        options.types = ','.join(args)
    [path, wp, fen, used_flightmodes, mav_type] = mavflightview.mavflightview_mav(mestate.mlog, options, mestate.flightmode_selections)
    child = multiproc.Process(target=mavflightview.mavflightview_show, args=[path, wp, fen, used_flightmodes, mav_type, options])
    child.start()
    mestate.mlog.rewind()

def cmd_set(args):
    '''control MAVExporer options'''
    mestate.settings.command(args)

def cmd_condition(args):
    '''control MAVExporer conditions'''
    if len(args) == 0:
        print("condition is: %s" % mestate.settings.condition)
        return
    mestate.settings.condition = ' '.join(args)
    if len(mestate.settings.condition) == 0 or mestate.settings.condition == 'clear':
        mestate.settings.condition = None

def cmd_reload(args):
    '''reload graphs'''
    mestate.console.writeln('Reloading graphs', fg='blue')
    load_graphs()
    setup_menus()
    mestate.console.write("Loaded %u graphs\n" % len(mestate.graphs))

def cmd_fft(args):
    '''display fft from log'''
    from MAVProxy.modules.lib import mav_fft
    if len(args) > 0:
        condition = args[0]
    else:
        condition = None
    child = multiproc.Process(target=mav_fft.mavfft_display, args=[mestate.filename,condition])
    child.start()

def save_graph(graphdef):
    '''save a graph as XML'''
    if graphdef.filename is None:
        if 'HOME' in os.environ:
            dname = os.path.join(os.environ['HOME'], '.mavproxy')
            if os.path.exists(dname):
                mp_util.mkdir_p(dname)
                graphdef.filename = os.path.join(dname, 'mavgraphs.xml')
        elif 'LOCALAPPDATA' in os.environ:
            dname = os.path.join(os.environ['LOCALAPPDATA'], 'MAVProxy')
            if os.path.exists(dname):
                mp_util.mkdir_p(dname)
                graphdef.filename = os.path.join(dname, 'mavgraphs.xml')
        else:
            graphdef.filename = 'mavgraphs.xml'
    if graphdef.filename is None:
        print("No file to save graph to")
        return
    try:
        graphs = load_graph_xml(open(graphdef.filename).read(), graphdef.filename, load_all=True)
    except Exception:
        graphs = []
    found_name = False
    for i in range(len(graphs)):
        if graphs[i].name == graphdef.name:
            graphs[i] = graphdef
            found_name = True
            break
    if not found_name:
        graphs.append(graphdef)
    pipe_console_input.send("Saving %u graphs to %s" % (len(graphs), graphdef.filename))
    f = open(graphdef.filename, "w")
    f.write("<graphs>\n\n")
    for g in graphs:
        f.write(" <graph name='%s'>\n" % g.name.strip())
        if g.description is None:
            g.description = ''
        f.write("  <description>%s</description>\n" % g.description.strip())
        for e in g.expressions:
            f.write("  <expression>%s</expression>\n" % e.strip())
        f.write(" </graph>\n\n")
    f.write("</graphs>\n")
    f.close()

def save_callback(operation, graphdef):
    '''callback from save thread'''
    if operation == 'test':
        for e in graphdef.expressions:
            if expression_ok(e, msgs):
                graphdef.expression = e
                pipe_graph_input.send('graph ' + graphdef.expression)
                return
        pipe_console_input.send('Invalid graph expressions')
        return
    if operation == 'save':
        save_graph(graphdef)

def save_process(MAVExpLastGraph, child_pipe_console_input, child_pipe_graph_input, statusMsgs):
    '''process for saving a graph'''
    from MAVProxy.modules.lib import wx_processguard
    from MAVProxy.modules.lib.wx_loader import wx
    from MAVProxy.modules.lib.wxgrapheditor import GraphDialog
    
    #This pipe is used to send text to the console
    global pipe_console_input
    pipe_console_input = child_pipe_console_input

    #This pipe is used to send graph commands
    global pipe_graph_input
    pipe_graph_input = child_pipe_graph_input
    
    #The valid expression messages, required to
    #validate the expression in the dialog box
    global msgs
    msgs = statusMsgs
    
    app = wx.App(False)
    if MAVExpLastGraph.description is None:
        MAVExpLastGraph.description = ''
    frame = GraphDialog('Graph Editor',
                        MAVExpLastGraph,
                        save_callback)
    frame.ShowModal()
    frame.Destroy()


def cmd_save(args):
    '''save a graph'''
    child = multiproc.Process(target=save_process, args=[mestate.last_graph, mestate.child_pipe_send_console, mestate.child_pipe_send_graph, mestate.status.msgs])
    child.start()

def cmd_messages(args):
    '''show messages'''
    if len(args) > 0:
        wildcard = args[0]
        if wildcard.find('*') == -1 and wildcard.find('?') == -1:
            wildcard = "*" + wildcard + "*"
    else:
        wildcard = '*'
    mestate.mlog.rewind()
    types = set(['MSG','STATUSTEXT'])
    while True:
        m = mestate.mlog.recv_match(type=types, condition=mestate.settings.condition)
        if m is None:
            break
        if m.get_type() == 'MSG':
            mstr = m.Message
        else:
            mstr = m.text
        if fnmatch.fnmatch(mstr.upper(), wildcard.upper()):
            tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m._timestamp))
            print("%s %s" % (tstr, mstr))
    mestate.mlog.rewind()

def cmd_param(args):
    '''show parameters'''
    if len(args) > 0:
        wildcard = args[0]
    else:
        wildcard = '*'
    k = sorted(mestate.mlog.params.keys())
    for p in k:
        if fnmatch.fnmatch(str(p).upper(), wildcard.upper()):
            print("%-16.16s %f" % (str(p), mestate.mlog.params[p]))

def cmd_devid(args):
    '''show parameters'''
    params = mestate.mlog.params
    k = sorted(params.keys())
    for p in k:
        if p.startswith('COMPASS_DEV_ID'):
            mp_util.decode_devid(params[p], p)
        if p.startswith('INS_') and p.endswith('_ID'):
            mp_util.decode_devid(params[p], p)

def cmd_loadfile(args):
    '''callback from menu to load a log file'''
    if len(args) != 1:
        fileargs = " ".join(args)
    else:
        fileargs = args[0]
    if not os.path.exists(fileargs):
        print("Error loading file ", fileargs);
        return
    if os.name == 'nt':
        #convert slashes in Windows
        fileargs = fileargs.replace("\\", "/")
    loadfile(fileargs.strip('"'))

def loadfile(args):
    '''load a log file (path given by arg)'''
    mestate.console.write("Loading %s...\n" % args)
    t0 = time.time()
    mlog = mavutil.mavlink_connection(args, notimestamps=False,
                                      zero_time_base=False,
                                      progress_callback=progress_bar)
    mestate.filename = args
    mestate.mlog = mlog
    mestate.status.msgs = mlog.messages
    t1 = time.time()
    mestate.console.write("\ndone (%u messages in %.1fs)\n" % (mestate.mlog._count, t1-t0))

    global flightmodes
    flightmodes = mlog.flightmode_list()

    load_graphs()
    setup_menus()

def process_stdin(line):
    '''handle commands from user'''
    if line is None:
        sys.exit(0)

    line = line.strip()
    if not line:
        return

    args = shlex.split(line)
    cmd = args[0]
    if cmd == 'help':
        k = command_map.keys()
        k.sort()
        for cmd in k:
            (fn, help) = command_map[cmd]
            print("%-15s : %s" % (cmd, help))
        return
    if cmd == 'exit':
        mestate.exit = True
        return

    if not cmd in command_map:
        print("Unknown command '%s'" % line)
        return
    (fn, help) = command_map[cmd]
    try:
        fn(args[1:])
    except Exception as e:
        print("ERROR in command %s: %s" % (args[1:], str(e)))

def input_loop():
    '''wait for user input'''
    while mestate.exit != True:
        try:
            if mestate.exit != True:
                line = input(mestate.rl.prompt)
        except EOFError:
            mestate.exit = True
            sys.exit(1)
        mestate.input_queue.put(line)

def main_loop():
    '''main processing loop, display graphs and maps'''
    global grui, last_xlim
    while True:
        if mestate is None or mestate.exit:
            return
        while not mestate.input_queue.empty():
            line = mestate.input_queue.get()
            cmds = line.split(';')
            for c in cmds:
                process_stdin(c)

        for i in range(0, len(grui)):
            xlim = grui[i].check_xlim_change()
            if xlim is not None and mestate.settings.sync_xzoom:
                remlist = []
                for j in range(0, len(grui)):
                    #print("set_xlim: ", j, xlim)
                    if not grui[j].set_xlim(xlim):
                        remlist.append(j)
                last_xlim = xlim
                if len(remlist) > 0:
                    # remove stale graphs
                    new_grui = []
                    for j in range(0, len(grui)):
                        if j not in remlist:
                            new_grui.append(grui[j])
                    grui = new_grui
                break

        time.sleep(0.1)


command_map = {
    'graph'      : (cmd_graph,     'display a graph'),
    'set'        : (cmd_set,       'control settings'),
    'reload'     : (cmd_reload,    'reload graphs'),
    'save'       : (cmd_save,      'save a graph'),
    'condition'  : (cmd_condition, 'set graph conditions'),
    'param'      : (cmd_param,     'show parameters'),
    'messages'   : (cmd_messages,  'show messages'),
    'devid'      : (cmd_devid,     'show device IDs'),
    'map'        : (cmd_map,       'show map view'),
    'fft'        : (cmd_fft,       'show a FFT (if available)'),
    'loadLog'    : (cmd_loadfile,  'load a log file'),
    }

def progress_bar(pct):
    if pct % 2 == 0:
        mestate.console.write('#')

if __name__ == "__main__":
    multiproc.freeze_support()
    from argparse import ArgumentParser
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--version", action='store_true', help="show version")
    parser.add_argument("files", metavar="<FILE>", nargs="?")
    args = parser.parse_args()

    if args.version:
        #pkg_resources doesn't work in the windows exe build, so read the version file
        try:
            import pkg_resources
            version = pkg_resources.require("mavproxy")[0].version
        except:
            start_script = os.path.join(os.environ['LOCALAPPDATA'], "MAVProxy", "version.txt")
            f = open(start_script, 'r')
            version = f.readline()
        print("MAVExplorer Version: " + version)
        sys.exit(1)
    
    mestate = MEState()
    setup_file_menu()

    mestate.rl = rline.rline("MAV> ", mestate)

    #If specified, open the log file
    if args.files is not None and len(args.files) != 0:
        loadfile(args.files)

    # run main loop as a thread
    mestate.thread = threading.Thread(target=main_loop, name='main_loop')
    mestate.thread.daemon = True
    mestate.thread.start()

    # input loop
    while mestate.rl is not None and mestate.exit != True:
        try:
            try:
                line = input(mestate.rl.prompt)
            except EOFError:
                mestate.exit = True
                break
            mestate.input_queue.put(line)
        except KeyboardInterrupt:
            mestate.exit = True
            break


#!/usr/bin/python

"CS244 Assignment 2: Buffer Sizing"

from mininet.topo import Topo
from mininet.node import CPULimitedHost
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.log import lg
from mininet.util import dumpNodeConnections
from mininet.cli import CLI

import subprocess
from subprocess import Popen, PIPE
from time import sleep, time
from multiprocessing import Process
import termcolor as T
from argparse import ArgumentParser

import sys
import os
from util.monitor import monitor_qlen
from util.helper import stdev


# Number of samples to skip for reference util calibration.
CALIBRATION_SKIP = 10

# Number of samples to grab for reference util calibration.
CALIBRATION_SAMPLES = 30

# Set the fraction of the link utilization that the measurement must exceed
# to be considered as having enough buffering.
TARGET_UTIL_FRACTION = 0.98

# Fraction of input bandwidth required to begin the experiment.
# At exactly 100%, the experiment may take awhile to start, or never start,
# because it effectively requires waiting for a measurement or link speed
# limiting error.
START_BW_FRACTION = 0.9

# Number of samples to take in get_rates() before returning.
NSAMPLES = 3

# Time to wait between samples, in seconds, as a float.
SAMPLE_PERIOD_SEC = 1.0

# Time to wait for first sample, in seconds, as a float.
SAMPLE_WAIT_SEC = 3.0


def cprint(s, color, cr=True):
    """Print in color
       s: string to print
       color: color to use"""
    if cr:
        print T.colored(s, color)
    else:
        print T.colored(s, color),


# Parse arguments

parser = ArgumentParser(description="Buffer sizing tests")
parser.add_argument('--bw-host', '-B',
                    dest="bw_host",
                    type=float,
                    action="store",
                    help="Bandwidth of host links",
                    required=True)

parser.add_argument('--bw-net', '-b',
                    dest="bw_net",
                    type=float,
                    action="store",
                    help="Bandwidth of network link",
                    required=True)

parser.add_argument('--delay',
                    dest="delay",
                    type=float,
                    help="Delay in milliseconds of host links",
                    default=87)

parser.add_argument('--dir', '-d',
                    dest="dir",
                    action="store",
                    help="Directory to store outputs",
                    default="results",
                    required=True)

parser.add_argument('-n',
                    dest="n",
                    type=int,
                    action="store",
                    help="Number of nodes in star.  Must be >= 3",
                    required=True)

parser.add_argument('--nflows',
                    dest="nflows",
                    action="store",
                    type=int,
                    help="Number of flows per host (for TCP)",
                    required=True)

parser.add_argument('--maxq',
                    dest="maxq",
                    action="store",
                    help="Max buffer size of network interface in packets",
                    default=1000)

parser.add_argument('--cong',
                    dest="cong",
                    help="Congestion control algorithm to use",
                    default="bic")

parser.add_argument('--target',
                    dest="target",
                    help="Target utilisation",
                    type=float,
                    default=TARGET_UTIL_FRACTION)

parser.add_argument('--iperf',
                    dest="iperf",
                    help="Path to custom iperf",
                    required=True)

# Expt parameters
args = parser.parse_args()

CUSTOM_IPERF_PATH = args.iperf
assert(os.path.exists(CUSTOM_IPERF_PATH))

if not os.path.exists(args.dir):
    os.makedirs(args.dir)

lg.setLogLevel('info')

# Topology to be instantiated in Mininet
class StarTopo(Topo):
    "Star topology for Buffer Sizing experiment"

    def __init__(self, n=3, cpu=None, bw_host=None, bw_net=None,
                 delay=None, maxq=None):
        # Add default members to class.
        super(StarTopo, self ).__init__()
        self.n = n
        self.cpu = cpu
        self.bw_host = bw_host
        self.bw_net = bw_net
        self.delay = delay
        self.maxq = maxq
        self.create_topology()

    # TODO: Fill in the following function to Create the experiment
    # topology Set appropriate values for bandwidth, delay, and queue
    # size.
    def create_topology(self):
        s0 = self.addSwitch('s0')
        h0 = self.addHost('h0')
        link_delay = float(args.delay)/2
        net_link = self.addLink(h0, s0, delay=('%fms' % link_delay), bw=self.bw_net, max_queue_size=self.maxq)

        for x in range(1, self.n):
            hx = self.addHost('h'+str(x))
            local_link = self.addLink(hx, s0, delay=('%fms' % link_delay), bw=self.bw_host, max_queue_size=self.maxq)
def start_tcpprobe():
    "Install tcp_probe module and dump to file"
    os.system("rmmod tcp_probe 2>/dev/null; modprobe tcp_probe;")
    Popen("cat /proc/net/tcpprobe > %s/tcp_probe.txt" %
          args.dir, shell=True)

def stop_tcpprobe():
    os.system("killall -9 cat; rmmod tcp_probe &>/dev/null;")

def count_connections():
    "Count current connections in iperf output file"
    out = args.dir + "/iperf_server.txt"
    lines = Popen("grep connected %s | wc -l" % out,
                  shell=True, stdout=PIPE).communicate()[0]
    return int(lines)

def set_q(iface, q):
    "Change queue size limit of interface"
    cmd = ("tc qdisc change dev %s parent 1:1 "
           "handle 10: netem limit %s" % (iface, q))
    #os.system(cmd)
    subprocess.check_output(cmd, shell=True)

def set_speed(iface, spd):
    "Change htb maximum rate for interface"
    cmd = ("tc class change dev %s parent 1:0 classid 1:1 "
           "htb rate %s burst 15k" % (iface, spd))
    os.system(cmd)

def get_txbytes(iface):
    f = open('/proc/net/dev', 'r')
    lines = f.readlines()
    for line in lines:
        if iface in line:
            break
    f.close()
    if not line:
        raise Exception("could not find iface %s in /proc/net/dev:%s" %
                        (iface, lines))
    # Extract TX bytes from:
    #Inter-|   Receive                                                |  Transmit
    # face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    # lo: 6175728   53444    0    0    0     0          0         0  6175728   53444    0    0    0     0       0          0
    return float(line.split()[9])

def get_rates(iface, nsamples=NSAMPLES, period=SAMPLE_PERIOD_SEC,
              wait=SAMPLE_WAIT_SEC):
    """Returns the interface @iface's current utilization in Mb/s.  It
    returns @nsamples samples, and each sample is the average
    utilization measured over @period time.  Before measuring it waits
    for @wait seconds to 'warm up'."""
    # Returning nsamples requires one extra to start the timer.
    nsamples += 1
    last_time = 0
    last_txbytes = 0
    ret = []
    sleep(wait)
    while nsamples:
        nsamples -= 1
        txbytes = get_txbytes(iface)
        now = time()
        elapsed = now - last_time
        #if last_time:
        #    print "elapsed: %0.4f" % (now - last_time)
        last_time = now
        # Get rate in Mbps; correct for elapsed time.
        rate = (txbytes - last_txbytes) * 8.0 / 1e6 / elapsed
        if last_txbytes != 0:
            # Wait for 1 second sample
            ret.append(rate)
        last_txbytes = txbytes
        print '.',
        sys.stdout.flush()
        sleep(period)
    return ret

def avg(s):
    "Compute average of list or string of values"
    if ',' in s:
        lst = [float(f) for f in s.split(',')]
    elif type(s) == str:
        lst = [float(s)]
    elif type(s) == list:
        lst = s
    return sum(lst)/len(lst)

def median(l):
    "Compute median from an unsorted list of values"
    s = sorted(l)
    if len(s) % 2 == 1:
        return s[(len(l) + 1) / 2 - 1]
    else:
        lower = s[len(l) / 2 - 1]
        upper = s[len(l) / 2]
        return float(lower + upper) / 2

def format_floats(lst):
    "Format list of floats to three decimal places"
    return ', '.join(['%.3f' % f for f in lst])

def ok(fraction):
    "Fraction is OK if it is >= args.target"
    return fraction >= args.target

def format_fraction(fraction):
    "Format and colorize fraction"
    if ok(fraction):
        return T.colored('%.3f' % fraction, 'green')
    return T.colored('%.3f' % fraction, 'red', attrs=["bold"])

def do_sweep(iface):
    """Sweep queue length until we hit target utilization.
       We assume a monotonic relationship and use a binary
       search to find a value that yields the desired result"""

    bdp = args.bw_net * 2 * args.delay * 1000.0 / 8.0 / 1500.0
    nflows = args.nflows * (args.n - 1)
    min_q, max_q = 1, int(bdp)

    # Set a higher speed on the bottleneck link in the beginning so
    # flows quickly connect
    set_speed(iface, "2Gbit")

    succeeded = 0
    wait_time = 300
    while wait_time > 0 and succeeded != nflows:
        wait_time -= 1
        succeeded = count_connections()
        print 'Connections %d/%d succeeded\r' % (succeeded, nflows),
        sys.stdout.flush()
        sleep(1)

    monitor = Process(target=monitor_qlen,
                      args=(iface, 0.01, '%s/qlen_%s.txt' %
                            (args.dir, iface)))
    monitor.start()

    if succeeded != nflows:
        print 'Giving up'
        return -1

    # TODO: Set the speed back to the bottleneck link speed.
    set_speed(iface, "%.2fMbit" % args.bw_net)
    print "\nSetting q=%d " % max_q,
    sys.stdout.flush()
    set_q(iface, max_q)

    # Wait till link is 100% utilised and train
    reference_rate = 0.0
    while reference_rate <= args.bw_net * START_BW_FRACTION:
        rates = get_rates(iface, nsamples=CALIBRATION_SAMPLES+CALIBRATION_SKIP)
        print "measured calibration rates: %s" % rates
        # Ignore first N; need to ramp up to full speed.
        rates = rates[CALIBRATION_SKIP:]
        reference_rate = median(rates)
        ru_max = max(rates)
        ru_stdev = stdev(rates)
        cprint ("Reference rate median: %.3f max: %.3f stdev: %.3f" %
                (reference_rate, ru_max, ru_stdev), 'blue')
        sys.stdout.flush()

    while abs(min_q - max_q) >= 2:
        mid = (min_q + max_q) / 2
        print "Trying q=%d  [%d,%d] " % (mid, min_q, max_q),
        sys.stdout.flush()

        set_q(iface, mid)

        rates=get_rates(iface, nsamples=CALIBRATION_SAMPLES+CALIBRATION_SKIP)
        rates=rates[CALIBRATION_SKIP:]
        this_rate = median(rates)
        print "rate: %s" % this_rate

        if ok(this_rate / reference_rate):
            max_q = mid
        else:
            min_q = mid + 1

    monitor.terminate()
    print "*** Minq for target: %d" % max_q
    return max_q

# TODO: Fill in the following function to verify the latency
# settings of your topology

def verify_latency(net):
    "verify link latency"
    print "Verifying latency..."
    h0 = net.getNodeByName('h0')
    h1 = net.getNodeByName('h1')
    h2 = net.getNodeByName('h2')
    h1RTT = net.pingFull(hosts=[h0, h1])[0][2][2]
    h2RTT = net.pingFull(hosts=[h0, h2])[0][2][2]

    target_RTT = args.delay*2
    if target_RTT-1 < h1RTT < target_RTT+1 and target_RTT-1 < h2RTT < target_RTT+1:
        print "Latency verified!"
    else:
        sys.exit("Latency not verified!")

# TODO: Fill in the following function to verify the bandwidth
# settings of your topology

def verify_bandwidth(net):
    "verify link bandwidth"
    print "Verifying bandwidth..."
    h0 = net.getNodeByName('h0')
    h1 = net.getNodeByName('h1')

    server = h0.popen("iperf -s -w 16m", shell=True)
    client = h1.popen("iperf -c "+h0.IP()+" -t "+ str(100), shell=True)
    rates = get_rates('s0-eth1', nsamples=CALIBRATION_SAMPLES+CALIBRATION_SKIP, period=0.5)
    h0.popen('killall iperf')
    h1.popen('killall iperf')
    server.terminate()
    client.terminate()

    rates = rates[CALIBRATION_SKIP:]
    if median(rates) / args.bw_net < START_BW_FRACTION:
        sys.exit("Bandwidth not verified!")
    else:
        print "Bandwidth verified!"
        print


# TODO: Fill in the following function to
# Start iperf on the receiver node
# Hint: use getNodeByName to get a handle on the sender node
# Hint: iperf command to start the receiver:
#       '%s -s -p %s > %s/iperf_server.txt' %
#        (CUSTOM_IPERF_PATH, 5001, args.dir)
# Note: The output file should be <args.dir>/iperf_server.txt
#       It will be used later in count_connections()

def start_receiver(net):
    h0 = net.getNodeByName('h0')
    h0.popen('iperf %s -s -p %s > %s/iperf_server.txt' % (CUSTOM_IPERF_PATH, 5001, args.dir), shell=True)

# TODO: Fill in the following function to
# Start args.nflows flows across the senders in a round-robin fashion
# Hint: use getNodeByName to get a handle on the sender (A or B in the
# figure) and receiver node (C in the figure).
# Hint: iperf command to start flow:
#       '%s -c %s -p %s -t %d -i 1 -yc -Z %s > %s/%s' % (
#           CUSTOM_IPERF_PATH, server.IP(), 5001, seconds, args.cong, args.dir, output_file)
# It is a good practice to store output files in a place specific to the
# experiment, where you can easily access, e.g., under args.dir.
# It will be very handy when debugging.  You are not required to
# submit these in your final submission.

def start_senders(net):
    # Seconds to run iperf; keep this very high
    seconds = 7200
    h0 = net.getNodeByName('h0')
    h1 = net.getNodeByName('h1')
    h2 = net.getNodeByName('h2')
    for x in range(0, args.nflows):
        h1.popen('%s -c %s -p %s -t %d -i 1 -yc -Z %s > %s/iperf_h1.txt' % (CUSTOM_IPERF_PATH, h0.IP(), 5001, seconds, args.cong, args.dir), shell=True)
        h2.popen('%s -c %s -p %s -t %d -i 1 -yc -Z %s > %s/iperf_h2.txt' % (CUSTOM_IPERF_PATH, h0.IP(), 5001, seconds, args.cong, args.dir), shell=True)

def main():
    "Create network and run Buffer Sizing experiment"

    start = time()
    # Reset to known state
    topo = StarTopo(n=args.n, bw_host=args.bw_host,
                    delay='%sms' % args.delay,
                    bw_net=args.bw_net, maxq=args.maxq)
    net = Mininet(topo=topo, host=CPULimitedHost, link=TCLink)
    net.start()
    dumpNodeConnections(net.hosts)
    net.pingAll()

    # TODO: verify latency and bandwidth of links in the topology you
    # just created.
    verify_latency(net)
    verify_bandwidth(net)

    start_receiver(net)

    start_tcpprobe()

    cprint("Starting experiment", "green")

    start_senders(net)

    # TODO: change the interface for which queue size is adjusted
    ret = do_sweep(iface='s0-eth1')
    total_flows = (args.n - 1) * args.nflows

    # Store output.  It will be parsed by run.sh after the entire
    # sweep is completed.  Do not change this filename!
    output = "%d %s %.3f\n" % (total_flows, ret, ret * 1500.0)
    open("%s/result.txt" % args.dir, "w").write(output)

    # Shut down iperf processes
    os.system('killall -9 ' + CUSTOM_IPERF_PATH)

    net.stop()
    Popen("killall -9 top bwm-ng tcpdump cat mnexec", shell=True).wait()
    stop_tcpprobe()
    end = time()
    cprint("Sweep took %.3f seconds" % (end - start), "yellow")

if __name__ == '__main__':
    try:
        main()
    except:
        print "-"*80
        print "Caught exception.  Cleaning up..."
        print "-"*80
        import traceback
        traceback.print_exc()
        os.system("killall -9 top bwm-ng tcpdump cat mnexec iperf; mn -c")


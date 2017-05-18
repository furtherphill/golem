from __future__ import division

import atexit
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from distutils.version import StrictVersion
from os import path

import requests
import sys
from ethereum.keys import privtoaddr
from ethereum.transactions import Transaction
from ethereum.utils import normalize_address, denoms
from web3 import Web3, IPCProvider
from web3.providers.ipc import get_default_ipc_path

from golem.core.common import is_windows
from golem.core.crypto import privtopub
from golem.environments.utils import find_program

log = logging.getLogger('golem.ethereum')


def ropsten_faucet_donate(addr):
    addr = normalize_address(addr)
    URL_TEMPLATE = "http://188.165.227.180:4000/donate/{}"
    request = URL_TEMPLATE.format(addr.encode('hex'))
    response = requests.get(request)
    if response.status_code != 200:
        log.error("Ropsten Faucet error code {}".format(response.status_code))
        return False
    response = response.json()
    if response['paydate'] == 0:
        log.warning("Ropsten Faucet warning {}".format(response['message']))
        return False
    # The paydate is not actually very reliable, usually some day in the past.
    paydate = datetime.fromtimestamp(response['paydate'])
    amount = int(response['amount']) / denoms.ether
    log.info("Faucet: {:.6f} ETH on {}".format(amount, paydate))
    return True


class Faucet(object):
    PRIVKEY = "{:32}".format("Golem Faucet")
    PUBKEY = privtopub(PRIVKEY)
    ADDR = privtoaddr(PRIVKEY)

    @staticmethod
    def gimme_money(ethnode, addr, value):
        nonce = ethnode.get_transaction_count('0x' + Faucet.ADDR.encode('hex'))
        addr = normalize_address(addr)
        tx = Transaction(nonce, 1, 21000, addr, value, '')
        tx.sign(Faucet.PRIVKEY)
        h = ethnode.send(tx)
        log.info("Faucet --({} ETH)--> {} ({})".format(value / denoms.ether,
                                                       '0x' + addr.encode('hex'), h))
        h = h[2:].decode('hex')
        return h


class NodeProcess(object):
    MIN_GETH_VERSION = '1.6.1'
    MAX_GETH_VERSION = '1.6.999'
    BOOT_NODES = [
        "enode://a24ac7c5484ef4ed0c5eb2d36620ba4e4aa13b8c84684e1b4aab0cebea2ae45cb4d375b77eab56516d34bfbd3c1a833fc51296ff084b770b94fb9028c4d25ccf@52.169.42.101:30303?discport=30304",  #noqa
    ]

    testnet = True

    def __init__(self, datadir):
        self.datadir = datadir
        log.info("Find geth node or start our own")
        self.__prog = find_program('geth')
        if not self.__prog:
            raise OSError("Ethereum client 'geth' not found")
        output, _ = subprocess.Popen([self.__prog, 'version'],
                                     stdout=subprocess.PIPE).communicate()
        ver = StrictVersion(re.search("Version: (\d+\.\d+\.\d+)", output).group(1))
        if ver < self.MIN_GETH_VERSION or ver > self.MAX_GETH_VERSION:
            raise OSError("Incompatible Ethereum client 'geth' version: {}".format(ver))
        log.info("geth version {}".format(ver))

        self.__ps = None  # child process

    def is_running(self):
        return self.__ps is not None

    def start(self):
        if self.__ps is not None:
            raise RuntimeError("Ethereum node already started by us")

        # Init geth datadir
        chain = 'rinkeby'
        geth_datadir = path.join(self.datadir, 'ethereum', chain)
        datadir_arg = '--datadir={}'.format(geth_datadir)
        this_dir = path.dirname(__file__)
        init_file = path.join(this_dir, chain + '.json')
        log.info("init file: {}".format(init_file))

        init_subp = subprocess.Popen([
            self.__prog,
            datadir_arg,
            'init', init_file
        ])
        init_subp.wait()
        if init_subp.returncode != 0:
            raise OSError(
                "geth init failed with code {}".format(init_subp.returncode))

        log.info("Will attempt to start new Ethereum node")

        args = [
            self.__prog,
            datadir_arg,
            '--cache=32',
            '--syncmode=light',
            '--networkid=4',
            '--bootnodes', ','.join(self.BOOT_NODES),
            '--verbosity', '3',
        ]

        self.__ps = subprocess.Popen(args, close_fds=True)
        atexit.register(lambda: self.stop())
        WAIT_PERIOD = 0.1
        wait_time = 0
        ipc_path = path.join(geth_datadir, 'geth.ipc')
        self.web3 = Web3(IPCProvider(ipc_path))
        while not self.web3.isConnected():
            # FIXME: Add timeout limit, we don't want to loop here forever.
            time.sleep(WAIT_PERIOD)
            wait_time += WAIT_PERIOD

        identified_chain = self.identify_chain()
        if identified_chain != chain:
            raise OSError("Wrong '{}' Ethereum chain".format(identified_chain))

        log.info("Node started in {} s: `{}`".format(wait_time, " ".join(args)))

    def stop(self):
        if self.__ps:
            start_time = time.clock()

            try:
                self.__ps.terminate()
                self.__ps.wait()
            except subprocess.NoSuchProcess:
                log.warn("Cannot terminate node: process {} no longer exists"
                         .format(self.__ps.pid))

            self.__ps = None
            duration = time.clock() - start_time
            log.info("Node terminated in {:.2f} s".format(duration))

    def identify_chain(self):
        """Check what chain the Ethereum node is running."""
        GENESES = {
            u'0xd4e56740f876aef8c010b86a40d5f56745a118d0906a34e69aec8c0db1cb8fa3': 'mainnet',
            u'0x41941023680923e0fe4d74a34bdac8141f2540e3ae90623718e47d66d1ca4a2d': 'ropsten',
            u'0x6341fd3daf94b748c72ced5a5b26028f2474f5f00d824504e4fa37a75767e177': 'rinkeby',
        }
        genesis = self.web3.eth.getBlock(0)['hash']
        chain = GENESES.get(genesis, 'unknown')
        log.info("{} chain ({})".format(chain, genesis))
        return chain


def is_geth_listening(testnet):
    # FIXME: Use web3 from Node object
    web3 = Web3(IPCProvider(testnet=testnet))
    return web3.isConnected()


def get_default_geth_path(testnet=False):
    if sys.platform == 'win32':
        return os.path.expanduser(os.path.join(
            "~",
            "AppData",
            "Roaming",
            "Ethereum",
            "testnet" if testnet else ""
        ))
    else:
        # if not using Named Pipes, remove "geth.ipc" from the returned path
        return os.path.dirname(get_default_ipc_path(testnet))

#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# pylint: disable=missing-docstring

import argparse
import json
import os
import sha3
import sys
import time
import yaml
import binascii

from ethereum.abi import ContractTranslator
import ethereum.tools.tester as eth_tester
import ethereum.tools._solidity as solidity

from create_init_data import dictlist_to_ordereddict

DEFAULT_PREVHASH = '0x{:064x}'.format(0)
BLOCK_GAS_LIMIT = 471238800


def function_encode(func_sign):
    keccak = sha3.keccak_256()
    keccak.update(func_sign.encode('utf-8'))
    return binascii.unhexlify(keccak.hexdigest()[0:8])


class GenesisData(object):
    # pylint: disable=too-many-instance-attributes,too-many-arguments
    def __init__(
            self, contracts_dir, contracts_docs_dir, init_data_file,
            timestamp, prevhash):
        self.timestamp = int(time.time() * 1000) if not timestamp else timestamp
        self.prevhash = DEFAULT_PREVHASH if not prevhash else prevhash

        self.contracts_dir = contracts_dir
        self.contracts_docs_dir = contracts_docs_dir
        self.contracts_common_dir = os.path.join(self.contracts_dir, 'common')
        contracts_list_file = os.path.join(contracts_dir, 'contracts.yml')
        self.load_contracts_list(contracts_list_file)
        self.load_contracts_args(init_data_file)

        self.init_chain_tester()

        self.accounts = dict()

    def load_contracts_list(self, contracts_list_file):
        """From file to load the list of contracts."""
        with open(contracts_list_file, 'r') as stream:
            contracts_list = yaml.load(stream)
        contracts_list['NormalContracts'] = dictlist_to_ordereddict(
            contracts_list['NormalContracts'])
        contracts_list['PermissionContracts']['basic'] \
            = dictlist_to_ordereddict(
                contracts_list['PermissionContracts']['basic'])
        contracts_list['PermissionContracts']['contracts'] \
            = dictlist_to_ordereddict(
                contracts_list['PermissionContracts']['contracts'])
        self.contracts_list = contracts_list

    def load_contracts_args(self, init_data_file):
        """From file to load arguments for contracts."""
        with open(init_data_file, 'r') as stream:
            data = yaml.load(stream)
        contracts_args = dictlist_to_ordereddict(data['Contracts'])
        for name, arguments in contracts_args.items():
            contracts_args[name] = dictlist_to_ordereddict(arguments)
        self.contracts_args = contracts_args

    def init_chain_tester(self):
        """Init a chain tester."""
        chain_env = eth_tester.get_env(None)
        chain_env.config['BLOCK_GAS_LIMIT'] = BLOCK_GAS_LIMIT
        self.chain_tester = eth_tester.Chain(env=chain_env)

    def compile_to_data(self, name, path):
        """Compile a solidity file and return the result data."""
        compiled = solidity.compile_file(
            path,
            combined='bin,abi,userdoc,devdoc,hashes',
            extra_args='common={}'.format(self.contracts_common_dir))
        data = solidity.solidity_get_contract_data(compiled, path, name)
        if not data['bin']:
            sys.exit(1)
        return data

    def write_docs(self, name, data):
        """Save userdoc, devdoc and hashes of contract function."""
        if self.contracts_docs_dir:
            for doc_type in ('userdoc', 'devdoc', 'hashes'):
                doc_file = os.path.join(self.contracts_docs_dir,
                                        '{}-{}.json'.format(name, doc_type))
                with open(doc_file, 'w') as stream:
                    json.dump(data[doc_type], stream, separators=(',', ': '),indent=4)

    def mine_contract_on_chain_tester(self, addr, code):
        """Mine in test chain to get data of a contract."""
        addr_in_tester = self.chain_tester.contract(
            code, language='evm', startgas=30000000)
        self.chain_tester.mine()
        account_in_tester = self.chain_tester \
              .chain.state.account_to_dict(addr_in_tester)
        self.accounts[addr] = {
            key: val
            for (key, val) in filter(
                lambda keyval: keyval[0] in ('code', 'storage', 'nonce'),
                account_in_tester.items(),
            )
        }

    def init_normal_contracts(self):
        """Compile normal contracts from files and construct by arguments.
        """
        ncinfo = self.contracts_list['NormalContracts']
        for name, info in ncinfo.items():
            addr = info['address']
            path = os.path.join(self.contracts_dir, info['file'])
            data = self.compile_to_data(name, path)
            self.write_docs(name, data)
            ctt = ContractTranslator(data['abi'])
            args = self.contracts_args.get(name)
            extra = b'' if not args else ctt.encode_constructor_arguments(
                [arg for arg in args.values()])
            self.mine_contract_on_chain_tester(addr, data['bin'] + extra)

    def init_permission_contracts(self):
        ncinfo = self.contracts_list['NormalContracts']
        pcinfo = self.contracts_list['PermissionContracts']
        path = os.path.join(self.contracts_dir, pcinfo['file'])
        data = self.compile_to_data('Permission', path)
        self.write_docs('Permission', data)
        for name, info in pcinfo['basic'].items():
            addr = info['address']
            conts = [addr]
            funcs = [binascii.unhexlify('00000000')]
            ctt = ContractTranslator(data['abi'])
            extra = ctt.encode_constructor_arguments([name, conts, funcs])
            self.mine_contract_on_chain_tester(addr, data['bin'] + extra)
        for name, info in pcinfo['contracts'].items():
            addr = info['address']
            conts = [ncinfo[cont]['address'] for cont in info['contracts']]
            funcs = [function_encode(func) for func in info['functions']]
            ctt = ContractTranslator(data['abi'])
            extra = ctt.encode_constructor_arguments([name, conts, funcs])
            self.mine_contract_on_chain_tester(addr, data['bin'] + extra)

    def save_to_file(self, filepath):
        with open(filepath, 'w') as stream:
            json.dump(
                dict(
                    timestamp=self.timestamp,
                    prevhash=self.prevhash,
                    alloc=self.accounts,
                ),
                stream,
                separators=(',', ': '),
                indent=4)


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--contracts_dir', required=True, help='The directory of contracts.')
    parser.add_argument(
        '--contracts_docs_dir',
        help='The directory of generated documents for contracts.'
        ' If did not be specified, no documents will be generated.')
    parser.add_argument(
        '--init_data_file',
        required=True,
        help='Path of the file for initialization data of contracts.')
    parser.add_argument(
        '--output', required=True, help='Path of the output file.')
    parser.add_argument(
        '--timestamp', type=int, help='Specify a timestamp to use.')
    parser.add_argument('--prevhash', help='Prevhash of genesis.')
    args = parser.parse_args()
    return dict(
        contracts_dir=args.contracts_dir,
        contracts_docs_dir=args.contracts_docs_dir,
        init_data_file=args.init_data_file,
        output=args.output,
        timestamp=args.timestamp,
        prevhash=args.prevhash,
    )


def core(contracts_dir, contracts_docs_dir, init_data_file, output, timestamp,
         prevhash):
    # pylint: disable=too-many-arguments
    if solidity.get_solidity() is None:
        print('Solidity not found!')
        sys.exit(1)
    if contracts_docs_dir:
        contracts_docs_dir = os.path.abspath(contracts_docs_dir)
    genesis_data = GenesisData(
        os.path.abspath(contracts_dir),
        contracts_docs_dir,
        os.path.abspath(init_data_file),
        timestamp,
        prevhash,
    )
    genesis_data.init_normal_contracts()
    genesis_data.init_permission_contracts()
    genesis_data.save_to_file(output)


if __name__ == '__main__':
    core(**parse_arguments())

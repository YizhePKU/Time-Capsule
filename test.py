from google.protobuf.empty_pb2 import Empty
from bdware.bdledger.api.query_pb2_grpc import QueryStub
from bdware.bdledger.api import query_pb2
from bdware.bdledger.api.ledger_pb2_grpc import LedgerStub
from bdware.bdledger.api import ledger_pb2

import grpc

chan = grpc.insecure_channel('39.104.201.40:21121')
ledger_stub = LedgerStub(chan)
query_stub = QueryStub(chan)

response = ledger_stub.GetLedgers(Empty())
ledgers = response.ledgers
# inc = query_pb2.IncludeTransactions.NONE
# request = query_pb2.RecentBlocksRequest(ledger="hi", count=10, include_transactions=inc)
# response = query_stub.GetRecentBlocks(request)

import grpc

from google.protobuf.empty_pb2 import Empty as EmptyRequest
from bdware.bdledger.api.query_pb2_grpc import QueryStub
from bdware.bdledger.api import query_pb2
from bdware.bdledger.api.ledger_pb2_grpc import LedgerStub
from bdware.bdledger.api import ledger_pb2

ledger = 'time_capsule'
good_key = b'\xb3\xeak\xaf\xa4h\xb4\xfa\xc7\x9d\x07\xe58\x1a\xa7\xdd\xc5\x0f\x06\x9e'
bad_key = b'\000\xeak\xaf\xa4h\xb4\xfa\xc7\x9d\x07\xe58\x1a\xa7\xdd\xc5\x0f\x06\x9e'

chan = grpc.insecure_channel('39.104.201.40:21121')
ledger_stub = LedgerStub(chan)
query_stub = QueryStub(chan)

def add(data: bytes) -> bytes:
    '''Add data to the ledger.
    Returns a key that can be used to retrive the data from the ledger.'''
    # Most fields of the transaction can be left blank
    transaction = ledger_pb2.SendTransactionRequest.Transaction(
        data=data
    )
    request = ledger_pb2.SendTransactionRequest(
        ledger=ledger,
        transaction=transaction,
    )
    response = ledger_stub.SendTransaction(request)
    return response.hash

def get(key: bytes) -> bytes:
    '''Retrive data from the ledger.
    Throws KeyError if no data is associated with given key.'''
    request = query_pb2.GetTransactionByHashRequest(
        ledger=ledger,
        hash=key,
    )
    try:
        response = query_stub.GetTransactionByHash(request)
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise KeyError("Invalid ledger key") from e
        else:
            raise
    return response.transaction.data

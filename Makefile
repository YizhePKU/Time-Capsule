# https://github.com/grpc/grpc/issues/9575#issuecomment-293934506
generate:
	python3 -m grpc_tools.protoc -Iproto/bdledger --python_out=. --grpc_python_out=. proto/**/**/**/**/*.proto
	python3 -m grpc_tools.protoc -Iproto --python_out=. --grpc_python_out=. proto/service/*.proto
	python3 -m grpc_tools.protoc -Iproto --python_out=. --grpc_python_out=. proto/worker/*.proto
	python3 -m grpc_tools.protoc -Iproto --python_out=. --grpc_python_out=. proto/storage/*.proto

clean:
	rm -rf bdware/ google/ service/

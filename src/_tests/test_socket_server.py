"""
Unit tests for the SocketDataServer using pytest.
"""

import pytest
import threading
import time
import socket
import struct
import numpy as np
from splash_timepix.socket_server import SocketDataServer


@pytest.fixture
def server_setup():
    """Set up test fixtures for the server."""
    server = SocketDataServer(host='localhost', port=9999, buffer_size=100)
    received_data = []
    
    def data_callback(data):
        received_data.extend(data.tolist())
    
    server.set_data_callback(data_callback)
    
    yield server, received_data
    
    # Teardown
    if server.running:
        server.stop()
    time.sleep(0.1)  # Give server time to stop
    
def test_server_start_stop(server_setup):
    """Test basic server start and stop functionality."""
    server, received_data = server_setup
    
    # Test starting
    assert not server.running
    server.start()
    assert server.running
    
    # Give server time to start
    time.sleep(0.1)
    
    # Test stopping
    server.stop()
    assert not server.running
    
def test_single_message(server_setup):
    """Test sending a single 5-byte message."""
    server, received_data = server_setup
    
    server.start()
    time.sleep(0.1)  # Give server time to start
    
    # Connect and send a message
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect(('localhost', 9999))
        
        # Send a 5-byte message: int(1234) + byte(5)
        message = struct.pack('<I', 1234) + bytes([5])
        client_socket.sendall(message)
        
        # Wait for processing
        time.sleep(0.2)
        
        # Check if data was received and processed
        data_array = server.get_data_array()
        assert len(data_array) == 1
        assert data_array[0] == 1234
        
        # Check callback was called
        assert len(received_data) == 1
        assert received_data[0] == 1234
        
    finally:
        client_socket.close()
    
def test_multiple_messages(server_setup):
    """Test sending multiple 5-byte messages."""
    server, received_data = server_setup
    
    server.start()
    time.sleep(0.1)
    
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect(('localhost', 9999))
        
        # Send multiple messages
        test_values = [100, 200, 300, 400, 500]
        for value in test_values:
            message = struct.pack('<I', value) + bytes([0])
            client_socket.sendall(message)
        
        # Wait for processing
        time.sleep(0.3)
        
        # Check all data was received
        data_array = server.get_data_array()
        assert len(data_array) == len(test_values)
        
        # Check values match (order might be different due to threading)
        received_values = sorted(data_array.tolist())
        expected_values = sorted(test_values)
        assert received_values == expected_values
        
    finally:
        client_socket.close()
    
def test_data_array_operations(server_setup):
    """Test data array operations."""
    server, received_data = server_setup
    
    # Test initial state
    data = server.get_data_array()
    assert len(data) == 0
    
    # Test clearing (should not fail on empty array)
    server.clear_data_array()
    data = server.get_data_array()
    assert len(data) == 0
    
    # Add some data and test clearing
    server.start()
    time.sleep(0.1)
    
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect(('localhost', 9999))
        message = struct.pack('<I', 999) + bytes([0])
        client_socket.sendall(message)
        time.sleep(0.2)
        
        # Check data exists
        data = server.get_data_array()
        assert len(data) == 1
        
        # Clear and check
        server.clear_data_array()
        data = server.get_data_array()
        assert len(data) == 0
        
    finally:
        client_socket.close()
    
def test_queue_size(server_setup):
    """Test queue size reporting."""
    server, received_data = server_setup
    
    initial_size = server.get_queue_size()
    assert initial_size == 0
    
    # Note: Testing queue size with actual messages would be complex
    # due to the fast processing, so we just test the method exists


# Additional pytest-specific tests
def test_server_configuration():
    """Test server configuration parameters."""
    server = SocketDataServer(host='127.0.0.1', port=8080, buffer_size=500)
    
    assert server.host == '127.0.0.1'
    assert server.port == 8080
    assert server.buffer_size == 500
    assert not server.running
    

@pytest.mark.parametrize("test_value,extra_byte", [
    (0, 0),
    (42, 255),
    (1000000, 128),
    (4294967295, 1),  # Max uint32
])
def test_parametrized_messages(server_setup, test_value, extra_byte):
    """Test various message values using parametrized testing."""
    server, received_data = server_setup
    
    server.start()
    time.sleep(0.1)
    
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect(('localhost', 9999))
        
        # Send message with test parameters
        message = struct.pack('<I', test_value) + bytes([extra_byte])
        client_socket.sendall(message)
        
        # Wait for processing
        time.sleep(0.2)
        
        # Verify the value was processed correctly
        data_array = server.get_data_array()
        assert len(data_array) == 1
        assert data_array[0] == test_value
        
    finally:
        client_socket.close()


def test_server_callback_error_handling(server_setup):
    """Test that server handles callback errors gracefully."""
    server, received_data = server_setup
    
    # Set a callback that raises an exception
    def bad_callback(data):
        raise ValueError("Test error in callback")
    
    server.set_data_callback(bad_callback)
    server.start()
    time.sleep(0.1)
    
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect(('localhost', 9999))
        
        message = struct.pack('<I', 123) + bytes([0])
        client_socket.sendall(message)
        
        # Wait for processing
        time.sleep(0.2)
        
        # Server should still be running and data should be processed
        # even though callback failed
        assert server.running
        data_array = server.get_data_array()
        assert len(data_array) == 1
        assert data_array[0] == 123
        
    finally:
        client_socket.close()


@pytest.fixture
def temp_server():
    """Fixture for tests that need a clean server instance."""
    server = SocketDataServer(host='localhost', port=9998, buffer_size=50)
    yield server
    if server.running:
        server.stop()
    time.sleep(0.1)


def test_concurrent_clients(temp_server):
    """Test multiple clients sending data concurrently."""
    server = temp_server
    server.start()
    time.sleep(0.1)
    
    def send_messages(client_id, num_messages):
        """Send messages from a single client."""
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client_socket.connect(('localhost', 9998))
            for i in range(num_messages):
                value = client_id * 1000 + i
                message = struct.pack('<I', value) + bytes([client_id])
                client_socket.sendall(message)
                time.sleep(0.01)  # Small delay between messages
        finally:
            client_socket.close()
    
    # Start multiple client threads
    threads = []
    for client_id in range(3):
        thread = threading.Thread(target=send_messages, args=(client_id, 5))
        threads.append(thread)
        thread.start()
    
    # Wait for all clients to finish
    for thread in threads:
        thread.join()
    
    # Wait for processing
    time.sleep(0.5)
    
    # Check that all messages were received
    data_array = server.get_data_array()
    assert len(data_array) == 15  # 3 clients * 5 messages each


if __name__ == '__main__':
    pytest.main([__file__])

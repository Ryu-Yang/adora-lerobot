import torch

# 形状：(2, 3)
tensor1 = torch.tensor([[1, 2, 3],
                        [4, 5, 6]])

# 形状：(2, 2)
tensor2 = torch.tensor([[7, 8],
                        [9, 10]])

# 沿列方向拼接（dim=1）
result = torch.cat([tensor1, tensor2], dim=0)
print(result)